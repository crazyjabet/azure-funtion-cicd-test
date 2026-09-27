import json

import azure.functions as func
import pytest

import function_app
import hubspot_secrets


class _FakeOut:
    """Minimal concrete stand-in for func.Out — that class is abstract, so
    it can't be instantiated directly in tests."""

    def __init__(self):
        self.value = None

    def set(self, value):
        self.value = value

    def get(self):
        return self.value


def _http_request(body) -> func.HttpRequest:
    return func.HttpRequest(
        method="POST",
        url="https://func-internal-host/api/trigger",
        headers={},
        body=json.dumps(body).encode("utf-8"),
    )


def test_http_trigger_enqueues_bulk_job_when_object_id_omitted():
    out = _FakeOut()
    resp = function_app.http_trigger(_http_request({"object_type": "companies"}), out)

    assert resp.status_code == 202
    assert json.loads(out.value) == {"object_type": "companies", "object_id": None}


def test_http_trigger_enqueues_single_job_when_object_id_given():
    out = _FakeOut()
    resp = function_app.http_trigger(_http_request({"object_type": "contacts", "object_id": "123"}), out)

    assert resp.status_code == 202
    assert json.loads(out.value) == {"object_type": "contacts", "object_id": "123"}


def test_http_trigger_rejects_unknown_object_type():
    out = _FakeOut()
    resp = function_app.http_trigger(_http_request({"object_type": "widgets"}), out)

    assert resp.status_code == 400
    assert out.value is None


def test_http_trigger_rejects_empty_string_object_id():
    # "" must not be silently treated as "omitted" (which would mean a
    # full bulk fetch instead of the targeted single-record request it
    # looks like) — see queue_worker's matching guard.
    out = _FakeOut()
    resp = function_app.http_trigger(_http_request({"object_type": "companies", "object_id": ""}), out)

    assert resp.status_code == 400
    assert out.value is None


def test_http_trigger_rejects_non_json_body():
    out = _FakeOut()
    req = func.HttpRequest(method="POST", url="https://func-internal-host/api/trigger", headers={}, body=b"not json")

    resp = function_app.http_trigger(req, out)

    assert resp.status_code == 400
    assert out.value is None


def test_queue_worker_fetches_single_object_when_object_id_present(monkeypatch):
    monkeypatch.setattr(hubspot_secrets, "get_hubspot_pat_token", lambda: "fake-token")
    calls = []

    def fake_get_object(ot, oid, token):
        calls.append((ot, oid, token))
        return {"id": oid}

    monkeypatch.setattr(function_app.hubspot_client, "get_object", fake_get_object)

    msg = func.QueueMessage(body=json.dumps({"object_type": "companies", "object_id": "999"}).encode("utf-8"))
    function_app.queue_worker(msg)

    assert calls == [("companies", "999", "fake-token")]


def test_queue_worker_rejects_malformed_job_missing_object_type(monkeypatch):
    monkeypatch.setattr(hubspot_secrets, "get_hubspot_pat_token", lambda: "fake-token")

    msg = func.QueueMessage(body=json.dumps({"object_id": "999"}).encode("utf-8"))

    with pytest.raises(ValueError):
        function_app.queue_worker(msg)


def test_queue_worker_treats_empty_string_object_id_as_bulk_not_single(monkeypatch):
    # Defense-in-depth: http_trigger already rejects "" before enqueueing,
    # but queue_worker shouldn't silently misinterpret it as "omitted"
    # either, in case a message ever bypasses http_trigger (manual replay,
    # a future second producer). Treating "" as present-but-invalid here
    # means it hits get_object with an empty id rather than silently
    # fanning out into a full bulk fetch.
    monkeypatch.setattr(hubspot_secrets, "get_hubspot_pat_token", lambda: "fake-token")
    calls = []
    monkeypatch.setattr(
        function_app.hubspot_client, "get_object", lambda ot, oid, token: calls.append((ot, oid)) or {"id": oid}
    )
    bulk_calls = []
    monkeypatch.setattr(function_app.hubspot_client, "iter_objects", lambda ot, token: bulk_calls.append(ot) or iter([]))

    msg = func.QueueMessage(body=json.dumps({"object_type": "companies", "object_id": ""}).encode("utf-8"))
    function_app.queue_worker(msg)

    assert calls == [("companies", "")]
    assert bulk_calls == []


def test_queue_worker_iterates_all_objects_when_object_id_absent(monkeypatch):
    monkeypatch.setattr(hubspot_secrets, "get_hubspot_pat_token", lambda: "fake-token")
    monkeypatch.setattr(
        function_app.hubspot_client,
        "iter_objects",
        lambda ot, token: iter([{"id": "1"}, {"id": "2"}]),
    )

    msg = func.QueueMessage(body=json.dumps({"object_type": "deals"}).encode("utf-8"))
    function_app.queue_worker(msg)  # no assertion needed beyond "doesn't raise" — deals aren't persisted


def test_queue_worker_writes_company_with_business_number_to_cosmos(monkeypatch):
    monkeypatch.setattr(hubspot_secrets, "get_hubspot_pat_token", lambda: "fake-token")
    company = {"id": "48737689691", "properties": {"tt_taiwan_vat_number": "12345678", "name": "Test Co"}}
    monkeypatch.setattr(function_app.hubspot_client, "get_object", lambda ot, oid, token: company)
    upserted = []
    monkeypatch.setattr(function_app.cosmos_writer, "upsert_company", lambda doc: upserted.append(doc))

    msg = func.QueueMessage(body=json.dumps({"object_type": "companies", "object_id": "48737689691"}).encode("utf-8"))
    function_app.queue_worker(msg)

    assert len(upserted) == 1
    assert upserted[0]["business_number"] == "12345678"


def test_queue_worker_skips_cosmos_write_for_company_without_business_number(monkeypatch):
    monkeypatch.setattr(hubspot_secrets, "get_hubspot_pat_token", lambda: "fake-token")
    company = {"id": "1", "properties": {"name": "No VAT Co"}}
    monkeypatch.setattr(function_app.hubspot_client, "get_object", lambda ot, oid, token: company)
    upserted = []
    monkeypatch.setattr(function_app.cosmos_writer, "upsert_company", lambda doc: upserted.append(doc))

    msg = func.QueueMessage(body=json.dumps({"object_type": "companies", "object_id": "1"}).encode("utf-8"))
    function_app.queue_worker(msg)

    assert upserted == []


def test_queue_worker_writes_credential_with_associated_company_business_number(monkeypatch):
    monkeypatch.setattr(hubspot_secrets, "get_hubspot_pat_token", lambda: "fake-token")
    contact = {"id": "241187768981", "properties": {"email": "a@b.com", "firstname": "A", "lastname": "B"}}

    monkeypatch.setattr(function_app.hubspot_client, "get_object", lambda ot, oid, token: contact)
    monkeypatch.setattr(function_app.hubspot_client, "get_associated_company_id", lambda contact_id, token: "999")
    monkeypatch.setattr(
        function_app.hubspot_client, "get_company_business_number", lambda company_id, token: "87654321"
    )
    upserted = []
    monkeypatch.setattr(function_app.cosmos_writer, "upsert_credential", lambda doc: upserted.append(doc))

    msg = func.QueueMessage(
        body=json.dumps({"object_type": "contacts", "object_id": "241187768981"}).encode("utf-8")
    )
    function_app.queue_worker(msg)

    assert len(upserted) == 1
    assert upserted[0]["contact_email"] == "a@b.com"
    assert upserted[0]["business_number"] == "87654321"


def test_queue_worker_memoizes_company_lookup_across_bulk_contacts(monkeypatch):
    monkeypatch.setattr(hubspot_secrets, "get_hubspot_pat_token", lambda: "fake-token")
    contacts = [
        {"id": "1", "properties": {"email": "a@b.com"}},
        {"id": "2", "properties": {"email": "c@d.com"}},
    ]
    monkeypatch.setattr(function_app.hubspot_client, "iter_objects", lambda ot, token: iter(contacts))
    monkeypatch.setattr(function_app.hubspot_client, "get_associated_company_id", lambda contact_id, token: "999")
    lookups = []

    def fake_lookup(company_id, token):
        lookups.append(company_id)
        return "87654321"

    monkeypatch.setattr(function_app.hubspot_client, "get_company_business_number", fake_lookup)
    monkeypatch.setattr(function_app.cosmos_writer, "upsert_credential", lambda doc: None)

    msg = func.QueueMessage(body=json.dumps({"object_type": "contacts"}).encode("utf-8"))
    function_app.queue_worker(msg)

    # Both contacts share company 999 — should only hit HubSpot once, not
    # once per contact.
    assert lookups == ["999"]


def test_queue_worker_writes_credential_with_none_business_number_when_no_association(monkeypatch):
    monkeypatch.setattr(hubspot_secrets, "get_hubspot_pat_token", lambda: "fake-token")
    contact = {"id": "1", "properties": {"email": "a@b.com"}}
    monkeypatch.setattr(function_app.hubspot_client, "get_object", lambda ot, oid, token: contact)
    monkeypatch.setattr(function_app.hubspot_client, "get_associated_company_id", lambda contact_id, token: None)
    upserted = []
    monkeypatch.setattr(function_app.cosmos_writer, "upsert_credential", lambda doc: upserted.append(doc))

    msg = func.QueueMessage(body=json.dumps({"object_type": "contacts", "object_id": "1"}).encode("utf-8"))
    function_app.queue_worker(msg)

    assert upserted[0]["business_number"] is None


def test_queue_worker_skips_cosmos_write_for_contact_without_email(monkeypatch):
    monkeypatch.setattr(hubspot_secrets, "get_hubspot_pat_token", lambda: "fake-token")
    contact = {"id": "1", "properties": {}}
    monkeypatch.setattr(function_app.hubspot_client, "get_object", lambda ot, oid, token: contact)
    upserted = []
    monkeypatch.setattr(function_app.cosmos_writer, "upsert_credential", lambda doc: upserted.append(doc))

    msg = func.QueueMessage(body=json.dumps({"object_type": "contacts", "object_id": "1"}).encode("utf-8"))
    function_app.queue_worker(msg)

    assert upserted == []
