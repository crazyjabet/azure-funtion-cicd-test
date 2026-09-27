"""
Pure mapping functions: raw HubSpot API objects -> Cosmos DB document shape,
per CosmosDB_Table_Schema.md (SA's schema handoff, 2026-09-11). No I/O here
— cosmos_writer.py does the actual reads/writes; keeping this pure makes it
testable without a Cosmos client or network access.

Fields intentionally NOT populated this round, left None/default per the
schema doc's own "待確認" list (not invented, not guessed):
- companies.tier: always "free" — HubSpot has no corresponding property yet
  (schema doc §4 item 1, 貿協 hasn't decided whether it lives on Company or
  Contact).
- companies.monthly_token_quota: always 0 — the tier→quota lookup table the
  schema doc describes ("中台依tier對照表產生") doesn't exist yet.
- companies.deal_id / deal_closed_at: always None — schema doc §4 item 5
  describes this as driven by subscribing to a Deal's hs_is_closed_won
  webhook event, not something a company fetch itself computes.
- companies.status, monthly_call_quota: no source specified in the schema
  doc at all ("—" / 中台維護) — left at the schema's own documented
  defaults.
- credentials.credential_issued / credential_status / expires_at: schema
  doc §4 items 3 and 6 — these come from a HubSpot field that doesn't exist
  yet (pending 貿協 adding it) and/or 中台-computed logic, not from a
  Contact fetch.

business_number COALESCE priority (tt_taiwan_vat_number > tt_global_vat_number
> tt_vat_number) is exactly what the schema doc specifies, but the doc's own
§4 item 2 flags that priority as unconfirmed with 貿協 — implemented as
documented, not as something this session decided.

Because of the above, every field listed there is a *default*, not
authoritative business state — the not-yet-built quota/tier/credential
business logic is expected to write real values into these same fields
later (companies.tier, credentials.credential_issued, etc.). This module
only produces what a single HubSpot fetch looks like in isolation; it's
cosmos_writer.py's job (not this module's) to merge that against whatever
already exists in Cosmos so a re-sync doesn't stomp on business state this
module never had any way to know about in the first place.
"""

import datetime


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def company_business_number(hubspot_company: dict) -> str | None:
    props = hubspot_company.get("properties", {})
    return props.get("tt_taiwan_vat_number") or props.get("tt_global_vat_number") or props.get("tt_vat_number")


# Fields cosmos_writer.py preserves from an existing document, if one
# exists, instead of letting this module's defaults stomp on them — none of
# these are sourced from HubSpot (see module docstring), so once real
# business logic sets them, a HubSpot re-sync must not reset them.
COMPANY_FIELDS_PRESERVED_IF_EXISTING = (
    "tier",
    "monthly_token_quota",
    "monthly_call_quota",
    "status",
    "deal_id",
    "deal_closed_at",
    "created_at",
)
CREDENTIAL_FIELDS_PRESERVED_IF_EXISTING = (
    "credential_issued",
    "credential_issued_at",
    "credential_status",
    "expires_at",
    "created_at",
)


def company_to_cosmos_doc(hubspot_company: dict) -> dict:
    """Maps a raw HubSpot company object (from hubspot_client.get_object/
    iter_objects) to a companies container document, in isolation — this
    function has no idea whether a document for this company already
    exists in Cosmos, so every field it returns is this module's default.
    cosmos_writer.py's upsert_company merges this against any existing
    document using COMPANY_FIELDS_PRESERVED_IF_EXISTING before writing.
    Returns a dict with business_number possibly None — callers must check
    before writing, since it's also the partition key (see schema doc §4
    item 2: source VAT fields are filled on under 50% of records)."""
    props = hubspot_company.get("properties", {})
    hubspot_company_id = int(hubspot_company["id"])
    now = _now_iso()
    return {
        # Deterministic, not the schema doc's illustrative random sample ID
        # ("c-8a2f1e") — makes re-syncing the same company an idempotent
        # upsert onto the same document instead of piling up duplicates.
        # Matches the id-derived-from-partition-key pattern the doc already
        # uses for credentials/quota_usage, just not spelled out for
        # companies specifically.
        "id": company_business_number(hubspot_company),
        "business_number": company_business_number(hubspot_company),
        "company_name": props.get("name"),
        "company_name_en": props.get("company_name_en"),
        "hubspot_company_id": hubspot_company_id,
        "tier": "free",
        "monthly_token_quota": 0,
        "monthly_call_quota": None,
        "status": None,
        "deal_id": None,
        "deal_closed_at": None,
        "created_at": now,
        "updated_at": now,
        # NOT "hubspot_webhook" (the schema doc's assumed default) — that
        # implies the write originated from the webhook receiver, which
        # isn't wired up yet (see CLAUDE.md's 2026-09-11 decision notes).
        # This reflects what's actually happening right now.
        "sync_source": "hubspot_sync",
    }


def credential_to_cosmos_doc(hubspot_contact: dict, business_number: str | None) -> dict:
    """Maps a raw HubSpot contact object plus its associated company's
    business_number (from hubspot_client.get_associated_company_id + a
    company lookup) to a credentials container document, in isolation —
    see company_to_cosmos_doc's docstring; the same reasoning applies here,
    merged by cosmos_writer.py's upsert_credential using
    CREDENTIAL_FIELDS_PRESERVED_IF_EXISTING. contact_email possibly None —
    callers must check before writing, since it's also id and the partition
    key (schema doc: contact.email fill rate 97.76%, not 100%)."""
    props = hubspot_contact.get("properties", {})
    contact_email = props.get("email")
    first = props.get("firstname") or ""
    last = props.get("lastname") or ""
    contact_name = (first + last) if (first or last) else None
    now = _now_iso()
    return {
        "id": contact_email,
        "contact_email": contact_email,
        "business_number": business_number,
        "hubspot_contact_id": int(hubspot_contact["id"]),
        "contact_name": contact_name,
        "credential_issued": False,
        "credential_issued_at": None,
        "credential_status": None,
        "expires_at": None,
        "created_at": now,
        "updated_at": now,
        "sync_source": "hubspot_sync",
    }
