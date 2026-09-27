from azure.cosmos.exceptions import CosmosResourceNotFoundError

import cosmos_writer


class _FakeContainer:
    def __init__(self, existing_by_key=None):
        self._existing_by_key = existing_by_key or {}
        self.upserted = []

    def read_item(self, item, partition_key):
        key = (item, partition_key)
        if key not in self._existing_by_key:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        return self._existing_by_key[key]

    def upsert_item(self, doc):
        self.upserted.append(dict(doc))


_NEW_COMPANY_DOC = {
    "id": "12345678",
    "business_number": "12345678",
    "company_name": "Acme",
    "tier": "free",
    "monthly_token_quota": 0,
    "monthly_call_quota": None,
    "status": None,
    "deal_id": None,
    "deal_closed_at": None,
    "created_at": "2026-09-11T00:00:00+00:00",
    "updated_at": "2026-09-11T00:00:00+00:00",
    "sync_source": "hubspot_sync",
}

_EXISTING_COMPANY_DOC = {
    "id": "12345678",
    "business_number": "12345678",
    "company_name": "Acme (old name)",
    "tier": "paid",  # set by business logic, not HubSpot — must survive a re-sync
    "monthly_token_quota": 2000000,
    "monthly_call_quota": None,
    "status": "active",
    "deal_id": 39820117,
    "deal_closed_at": "2026-08-10T03:12:00+00:00",
    "created_at": "2026-01-01T00:00:00+00:00",
    "updated_at": "2026-01-01T00:00:00+00:00",
    "sync_source": "hubspot_sync",
}

_NEW_CREDENTIAL_DOC = {
    "id": "a@b.com",
    "contact_email": "a@b.com",
    "business_number": "12345678",
    "credential_issued": False,
    "credential_issued_at": None,
    "credential_status": None,
    "expires_at": None,
    "created_at": "2026-09-11T00:00:00+00:00",
    "updated_at": "2026-09-11T00:00:00+00:00",
}

_EXISTING_CREDENTIAL_DOC = {
    "id": "a@b.com",
    "contact_email": "a@b.com",
    "credential_issued": True,  # approved by a human, not HubSpot — must survive a re-sync
    "credential_issued_at": "2026-02-01T00:00:00+00:00",
    "credential_status": "active",
    "expires_at": "2027-02-01T00:00:00+00:00",
    "created_at": "2026-02-01T00:00:00+00:00",
    "updated_at": "2026-02-01T00:00:00+00:00",
}


def test_upsert_company_writes_new_document_when_none_exists(monkeypatch):
    container = _FakeContainer()
    monkeypatch.setattr(cosmos_writer, "_get_container", lambda env_var: container)

    cosmos_writer.upsert_company(dict(_NEW_COMPANY_DOC))

    assert container.upserted == [_NEW_COMPANY_DOC]


def test_upsert_company_preserves_business_state_fields_from_existing_document(monkeypatch):
    container = _FakeContainer(existing_by_key={("12345678", "12345678"): _EXISTING_COMPANY_DOC})
    monkeypatch.setattr(cosmos_writer, "_get_container", lambda env_var: container)

    cosmos_writer.upsert_company(dict(_NEW_COMPANY_DOC))

    written = container.upserted[0]
    # Preserved from the existing document — never overwritten by a re-sync,
    # since none of these are sourced from HubSpot.
    assert written["tier"] == "paid"
    assert written["monthly_token_quota"] == 2000000
    assert written["status"] == "active"
    assert written["deal_id"] == 39820117
    assert written["deal_closed_at"] == "2026-08-10T03:12:00+00:00"
    assert written["created_at"] == "2026-01-01T00:00:00+00:00"
    # Not preserved — these ARE sourced from HubSpot, so the fresh fetch wins.
    assert written["company_name"] == "Acme"
    assert written["updated_at"] == "2026-09-11T00:00:00+00:00"


def test_upsert_credential_writes_new_document_when_none_exists(monkeypatch):
    container = _FakeContainer()
    monkeypatch.setattr(cosmos_writer, "_get_container", lambda env_var: container)

    cosmos_writer.upsert_credential(dict(_NEW_CREDENTIAL_DOC))

    assert container.upserted == [_NEW_CREDENTIAL_DOC]


def test_upsert_credential_preserves_business_state_fields_from_existing_document(monkeypatch):
    container = _FakeContainer(existing_by_key={("a@b.com", "a@b.com"): _EXISTING_CREDENTIAL_DOC})
    monkeypatch.setattr(cosmos_writer, "_get_container", lambda env_var: container)

    cosmos_writer.upsert_credential(dict(_NEW_CREDENTIAL_DOC))

    written = container.upserted[0]
    assert written["credential_issued"] is True
    assert written["credential_issued_at"] == "2026-02-01T00:00:00+00:00"
    assert written["credential_status"] == "active"
    assert written["expires_at"] == "2027-02-01T00:00:00+00:00"
    assert written["created_at"] == "2026-02-01T00:00:00+00:00"
    assert written["updated_at"] == "2026-09-11T00:00:00+00:00"
