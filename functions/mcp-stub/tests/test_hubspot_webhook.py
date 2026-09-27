import json
import logging

import azure.functions as func
import pytest

import hubspot_webhook

_API_KEY = "test-webhook-api-key"

# HubSpot Workflow custom webhook actions send a flat JSON body of only the
# properties the workflow author chose to include — no fixed field names, no
# nesting, not a batch array. Field names here are illustrative only.
_SAMPLE_PAYLOAD = {"email": "test@example.com", "company_name": "Acme Corp"}


def _request(body, headers: dict) -> func.HttpRequest:
    return func.HttpRequest(
        method="POST",
        url="https://func-internal-host/api/hubspot/webhook",
        headers=headers,
        body=json.dumps(body).encode("utf-8"),
    )


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch):
    monkeypatch.setattr(hubspot_webhook, "_API_KEY", _API_KEY)


def test_valid_api_key_accepted_and_returns_200():
    req = _request(_SAMPLE_PAYLOAD, headers={"Authorization": f"Bearer {_API_KEY}"})
    resp = hubspot_webhook.hubspot_webhook(req)
    assert resp.status_code == 200


def test_logs_payload_as_is(caplog):
    req = _request(_SAMPLE_PAYLOAD, headers={"Authorization": f"Bearer {_API_KEY}"})
    with caplog.at_level(logging.INFO):
        resp = hubspot_webhook.hubspot_webhook(req)
    assert resp.status_code == 200
    assert "test@example.com" in caplog.text
    assert "Acme Corp" in caplog.text


def test_missing_authorization_header_rejected():
    req = _request(_SAMPLE_PAYLOAD, headers={})
    resp = hubspot_webhook.hubspot_webhook(req)
    assert resp.status_code == 401
    assert json.loads(resp.get_body()) == {"error": "unauthorized"}


def test_wrong_api_key_rejected():
    req = _request(_SAMPLE_PAYLOAD, headers={"Authorization": "Bearer wrong-key"})
    resp = hubspot_webhook.hubspot_webhook(req)
    assert resp.status_code == 401


def test_missing_bearer_prefix_rejected():
    # A header equal to the raw key without "Bearer " must not validate —
    # this is the exact header string HubSpot's Workflow API Key auth sends,
    # not a bare token.
    req = _request(_SAMPLE_PAYLOAD, headers={"Authorization": _API_KEY})
    resp = hubspot_webhook.hubspot_webhook(req)
    assert resp.status_code == 401


def test_api_key_not_configured_rejects_even_with_correct_looking_header(monkeypatch):
    monkeypatch.setattr(hubspot_webhook, "_API_KEY", "")
    req = _request(_SAMPLE_PAYLOAD, headers={"Authorization": f"Bearer {_API_KEY}"})
    resp = hubspot_webhook.hubspot_webhook(req)
    assert resp.status_code == 401


def test_non_json_body_after_valid_key_returns_400():
    req = func.HttpRequest(
        method="POST",
        url="https://func-internal-host/api/hubspot/webhook",
        headers={"Authorization": f"Bearer {_API_KEY}"},
        body=b"not json",
    )
    resp = hubspot_webhook.hubspot_webhook(req)
    assert resp.status_code == 400


class _FakeResponse:
    def __init__(self, status_code=202):
        self.status_code = status_code
        self.text = ""


def test_relays_to_hubspot_sync_when_hs_object_id_present(monkeypatch):
    monkeypatch.setattr(hubspot_webhook, "_HUBSPOT_SYNC_TRIGGER_URL", "https://hubspot-sync/api/trigger")
    monkeypatch.setattr(hubspot_webhook, "_HUBSPOT_SYNC_FUNCTION_KEY", "func-key")
    calls = []

    def fake_post(url, params, json, timeout):
        calls.append((url, params, json))
        return _FakeResponse(202)

    monkeypatch.setattr(hubspot_webhook.requests, "post", fake_post)

    req = _request({"hs_object_id": "48737689710"}, headers={"Authorization": f"Bearer {_API_KEY}"})
    resp = hubspot_webhook.hubspot_webhook(req)

    assert resp.status_code == 200
    assert len(calls) == 1
    url, params, body = calls[0]
    assert url == "https://hubspot-sync/api/trigger"
    assert params == {"code": "func-key"}
    assert body == {"object_type": "companies", "object_id": "48737689710"}


def test_skips_relay_when_hs_object_id_missing(monkeypatch):
    calls = []
    monkeypatch.setattr(hubspot_webhook.requests, "post", lambda *a, **kw: calls.append(1))

    req = _request({"email": "test@example.com"}, headers={"Authorization": f"Bearer {_API_KEY}"})
    resp = hubspot_webhook.hubspot_webhook(req)

    assert resp.status_code == 200
    assert calls == []


def test_relay_failure_does_not_fail_the_response(monkeypatch):
    monkeypatch.setattr(hubspot_webhook, "_HUBSPOT_SYNC_TRIGGER_URL", "https://hubspot-sync/api/trigger")
    monkeypatch.setattr(hubspot_webhook, "_HUBSPOT_SYNC_FUNCTION_KEY", "func-key")

    def raise_connection_error(*args, **kwargs):
        raise hubspot_webhook.requests.RequestException("connection failed")

    monkeypatch.setattr(hubspot_webhook.requests, "post", raise_connection_error)

    req = _request({"hs_object_id": "48737689710"}, headers={"Authorization": f"Bearer {_API_KEY}"})
    resp = hubspot_webhook.hubspot_webhook(req)

    assert resp.status_code == 200


def test_relay_http_error_status_does_not_fail_the_response(monkeypatch):
    monkeypatch.setattr(hubspot_webhook, "_HUBSPOT_SYNC_TRIGGER_URL", "https://hubspot-sync/api/trigger")
    monkeypatch.setattr(hubspot_webhook, "_HUBSPOT_SYNC_FUNCTION_KEY", "func-key")
    monkeypatch.setattr(hubspot_webhook.requests, "post", lambda *a, **kw: _FakeResponse(500))

    req = _request({"hs_object_id": "48737689710"}, headers={"Authorization": f"Bearer {_API_KEY}"})
    resp = hubspot_webhook.hubspot_webhook(req)

    assert resp.status_code == 200


def test_skips_relay_when_trigger_url_not_configured(monkeypatch):
    monkeypatch.setattr(hubspot_webhook, "_HUBSPOT_SYNC_TRIGGER_URL", "")
    calls = []
    monkeypatch.setattr(hubspot_webhook.requests, "post", lambda *a, **kw: calls.append(1))

    req = _request({"hs_object_id": "48737689710"}, headers={"Authorization": f"Bearer {_API_KEY}"})
    resp = hubspot_webhook.hubspot_webhook(req)

    assert resp.status_code == 200
    assert calls == []
