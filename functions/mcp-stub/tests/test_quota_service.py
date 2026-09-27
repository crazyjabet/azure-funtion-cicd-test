from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

import quota_service


class _FakeContainer:
    """Mimics just enough of azure.cosmos's ContainerProxy for
    quota_service's read_item/create_item/upsert_item(etag=...) usage,
    including ETag-based optimistic concurrency (a real Cosmos container
    rejects an upsert whose etag doesn't match the document's current one,
    and rejects a create_item whose id/partition key already exists)."""

    def __init__(self, existing: dict | None = None):
        # keyed by (id, partition_key) -> doc (with "_etag")
        self._store: dict[tuple, dict] = dict(existing or {})
        self.upserted: list[dict] = []
        self._next_etag = 1

    def read_item(self, item, partition_key):
        key = (item, partition_key)
        if key not in self._store:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        return dict(self._store[key])

    def create_item(self, body):
        key = (body["id"], body["business_number"])
        if key in self._store:
            raise CosmosResourceExistsError(status_code=409, message="already exists")
        self._store_and_record(key, body)

    def upsert_item(self, body, etag=None, match_condition=None):
        key = (body["id"], body["business_number"])
        if match_condition is not None:
            current = self._store.get(key)
            current_etag = current["_etag"] if current else None
            if current_etag != etag:
                raise CosmosAccessConditionFailedError(status_code=412, message="etag mismatch")
        self._store_and_record(key, body)

    def _store_and_record(self, key, body):
        stored = dict(body)
        stored["_etag"] = f"etag-{self._next_etag}"
        self._next_etag += 1
        self._store[key] = stored
        self.upserted.append(dict(stored))


def _patch_container(monkeypatch, container: _FakeContainer):
    monkeypatch.setattr(quota_service, "_get_container", lambda: container)


def test_first_call_for_a_period_creates_new_doc_with_calls_used_1(monkeypatch):
    container = _FakeContainer()
    _patch_container(monkeypatch, container)

    result = quota_service.check_and_increment("12345678", "free")

    assert result.allowed is True
    assert result.used == 1
    assert result.quota == 10
    written = container.upserted[0]
    assert written["business_number"] == "12345678"
    assert written["calls_used"] == 1
    assert written["call_quota"] == 10
    assert written["tokens_used"] == 0
    assert written["source"] == "azure_functions"


def test_subsequent_call_within_quota_increments_existing_doc(monkeypatch):
    period = quota_service._current_period()
    doc_id = f"12345678_{period}"
    existing = {
        (doc_id, "12345678"): {
            "id": doc_id,
            "business_number": "12345678",
            "period": period,
            "tokens_used": 0,
            "calls_used": 3,
            "token_quota": None,
            "call_quota": 10,
            "last_updated_at": "2026-09-01T00:00:00+00:00",
            "source": "azure_functions",
            "_etag": "etag-orig",
        }
    }
    container = _FakeContainer(existing=existing)
    _patch_container(monkeypatch, container)

    result = quota_service.check_and_increment("12345678", "free")

    assert result.allowed is True
    assert result.used == 4
    assert container.upserted[0]["calls_used"] == 4


def test_call_at_quota_limit_is_rejected_and_nothing_is_written(monkeypatch):
    period = quota_service._current_period()
    doc_id = f"12345678_{period}"
    existing = {
        (doc_id, "12345678"): {
            "id": doc_id,
            "business_number": "12345678",
            "period": period,
            "tokens_used": 0,
            "calls_used": 10,  # already at the free-tier limit
            "token_quota": None,
            "call_quota": 10,
            "last_updated_at": "2026-09-01T00:00:00+00:00",
            "source": "azure_functions",
            "_etag": "etag-orig",
        }
    }
    container = _FakeContainer(existing=existing)
    _patch_container(monkeypatch, container)

    result = quota_service.check_and_increment("12345678", "free")

    assert result.allowed is False
    assert result.used == 10
    assert result.quota == 10
    assert container.upserted == []  # a rejected call must never be counted


def test_paid_tier_is_never_blocked_even_past_the_free_limit(monkeypatch):
    period = quota_service._current_period()
    doc_id = f"87654321_{period}"
    existing = {
        (doc_id, "87654321"): {
            "id": doc_id,
            "business_number": "87654321",
            "period": period,
            "tokens_used": 0,
            "calls_used": 500,
            "token_quota": None,
            "call_quota": None,
            "last_updated_at": "2026-09-01T00:00:00+00:00",
            "source": "azure_functions",
            "_etag": "etag-orig",
        }
    }
    container = _FakeContainer(existing=existing)
    _patch_container(monkeypatch, container)

    result = quota_service.check_and_increment("87654321", "paid")

    assert result.allowed is True
    assert result.quota is None
    assert result.used == 501


def test_unknown_tier_falls_back_to_the_free_default_quota(monkeypatch):
    container = _FakeContainer()
    _patch_container(monkeypatch, container)

    result = quota_service.check_and_increment("12345678", "some-future-tier-not-in-the-table")

    assert result.quota == quota_service.TIER_MONTHLY_CALL_QUOTA["free"]


def test_concurrent_first_write_conflict_is_retried_against_the_winner(monkeypatch):
    # Empty container -> quota_service takes the "new doc" path, which uses
    # create_item (not upsert_item) specifically so this race is detectable
    # (see quota_service.py's comment on why upsert_item alone isn't enough
    # here).
    container = _FakeContainer()
    _patch_container(monkeypatch, container)

    real_create = container.create_item
    calls = {"count": 0}

    def flaky_create(body):
        calls["count"] += 1
        if calls["count"] == 1:
            # Simulate a concurrent request winning the race to create this
            # period's document first.
            raise CosmosResourceExistsError(status_code=409, message="already exists")
        return real_create(body)

    monkeypatch.setattr(container, "create_item", flaky_create)

    result = quota_service.check_and_increment("12345678", "free")

    assert result.allowed is True
    assert result.used == 1
    assert calls["count"] == 2  # first attempt conflicted, second succeeded


def test_concurrent_update_conflict_is_retried_against_the_fresh_document(monkeypatch):
    period = quota_service._current_period()
    doc_id = f"12345678_{period}"
    existing = {
        (doc_id, "12345678"): {
            "id": doc_id,
            "business_number": "12345678",
            "period": period,
            "tokens_used": 0,
            "calls_used": 3,
            "token_quota": None,
            "call_quota": 10,
            "last_updated_at": "2026-09-01T00:00:00+00:00",
            "source": "azure_functions",
            "_etag": "etag-orig",
        }
    }
    container = _FakeContainer(existing=existing)
    _patch_container(monkeypatch, container)

    real_upsert = container.upsert_item
    calls = {"count": 0}

    def flaky_upsert(body, etag=None, match_condition=None):
        calls["count"] += 1
        if calls["count"] == 1:
            # Simulate someone else updating this doc between our read and
            # write — the real SDK raises this on an etag mismatch.
            raise CosmosAccessConditionFailedError(status_code=412, message="etag mismatch")
        return real_upsert(body, etag=etag, match_condition=match_condition)

    monkeypatch.setattr(container, "upsert_item", flaky_upsert)

    result = quota_service.check_and_increment("12345678", "free")

    assert result.allowed is True
    assert result.used == 4
    assert calls["count"] == 2  # first attempt conflicted, second succeeded
