"""
Per-tier monthly call quota enforcement against Cosmos DB's quota_usage
container (see docs/schema/CosmosDB_Table_Schema.md). Cosmos is the source
of truth for usage counts, not an in-process cache — this Function App runs
on Consumption plan, so instances spin up/down per traffic and any
in-memory counter would reset constantly and undercount.

Quota numbers in TIER_MONTHLY_CALL_QUOTA are placeholders, not a decided
business rule: the RFI document only gives them as an illustrative example
("EX:每月十次或Token使用量限制"), the same reason
hubspot_to_cosmos.company_to_cosmos_doc() defaults companies.monthly_token_quota
to 0 (中台's real tier→quota mapping table doesn't exist yet). User
confirmed 2026-09-12: use the RFI's own example values to get the
enforcement mechanism working now; the real numbers are a separate,
later business decision.

Token-based quota (quota_usage.tokens_used/token_quota) is intentionally
NOT enforced here: mcp_server.py's only tool (`echo`) doesn't call an LLM,
so there is no real token count to check against yet. Only calls_used is
tracked; tokens_used/token_quota stay at 0/None until a real AI-backed tool
exists to measure against.

Only `tools/call` JSON-RPC requests consume quota (see function_app.py) —
`initialize`/`tools/list`/`ping`/etc. are protocol handshake, not usage of
anything a member is being metered for.

Concurrency: check-then-increment uses the document's ETag as an optimistic
concurrency guard (Cosmos DB's standard mechanism) with a small retry loop,
not just a bare read-then-upsert — without it, two truly concurrent calls
from the same company could both read the same `calls_used`, both pass the
check, and both write, letting the count exceed quota by one. A handful of
retries is enough for this test environment's expected call volume (one
MCP client per member, not a burst of parallel requests); it is not meant
to hold up under high write contention.
"""

import datetime
import functools
import os
from dataclasses import dataclass

from azure.core import MatchConditions
from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from azure.identity import DefaultAzureCredential

# RFI 文件示意值（"EX:每月十次或Token使用量限制"），非正式決定的數字，
# 詳見本檔案開頭說明。None 代表不限制次數。
TIER_MONTHLY_CALL_QUOTA: dict[str, int | None] = {
    "free": 10,
    "paid": None,
}
# 未知/未對到上表的 tier 一律當 free 處理，比照 member_repository.py
# 「查不到就當 free、不當付費會員」的既有保守慣例。
_DEFAULT_TIER_QUOTA = TIER_MONTHLY_CALL_QUOTA["free"]

_MAX_RETRIES = 5


@dataclass(frozen=True)
class QuotaCheckResult:
    allowed: bool
    tier: str
    quota: int | None  # None = unlimited
    used: int  # 本次呼叫「之後」的累計用量（若 allowed=False，則是拒絕當下的既有用量，不含本次）


@functools.lru_cache(maxsize=1)
def _get_client() -> CosmosClient:
    return CosmosClient(os.environ["COSMOS_DB_ENDPOINT"], credential=DefaultAzureCredential())


def _get_container():
    database = _get_client().get_database_client(os.environ["COSMOS_DB_DATABASE_NAME"])
    return database.get_container_client(os.environ["COSMOS_DB_QUOTA_USAGE_CONTAINER_NAME"])


def _current_period() -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def check_and_increment(business_number: str, tier: str) -> QuotaCheckResult:
    """Checks the current period's call count against the tier's monthly
    quota and, only if still within quota, atomically increments and
    persists the new count. A rejected call (allowed=False) never writes
    anything, so it's never counted."""
    quota = TIER_MONTHLY_CALL_QUOTA.get(tier, _DEFAULT_TIER_QUOTA)
    period = _current_period()
    doc_id = f"{business_number}_{period}"
    container = _get_container()

    for _ in range(_MAX_RETRIES):
        try:
            doc = container.read_item(item=doc_id, partition_key=business_number)
            etag = doc["_etag"]
        except CosmosResourceNotFoundError:
            doc = None
            etag = None

        used_so_far = doc["calls_used"] if doc is not None else 0

        if quota is not None and used_so_far >= quota:
            return QuotaCheckResult(allowed=False, tier=tier, quota=quota, used=used_so_far)

        new_used = used_so_far + 1
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if doc is None:
            doc = {
                "id": doc_id,
                "business_number": business_number,
                "period": period,
                "tokens_used": 0,
                "calls_used": new_used,
                "token_quota": None,
                "call_quota": quota,
                "last_updated_at": now_iso,
                "source": "azure_functions",
            }
        else:
            doc["calls_used"] = new_used
            doc["call_quota"] = quota
            doc["last_updated_at"] = now_iso

        try:
            if etag is None:
                # create_item (not upsert_item) so a second, concurrent
                # first-ever-call for this period can't silently overwrite
                # the other's write — it raises CosmosResourceExistsError
                # instead, caught below and retried as a re-read against
                # whichever call actually won the race.
                container.create_item(doc)
            else:
                container.upsert_item(doc, etag=etag, match_condition=MatchConditions.IfNotModified)
            return QuotaCheckResult(allowed=True, tier=tier, quota=quota, used=new_used)
        except (CosmosAccessConditionFailedError, CosmosResourceExistsError):
            # Someone else wrote this document between our read and write —
            # re-read the fresh version and retry the whole check.
            continue

    raise RuntimeError(f"quota_service: gave up after {_MAX_RETRIES} concurrent-write retries for {doc_id}")
