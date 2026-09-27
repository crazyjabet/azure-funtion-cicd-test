import pytest

import hubspot_client


class _FakeResponse:
    def __init__(self, json_body, status_code=200):
        self._json_body = json_body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json_body


def test_get_object_rejects_unknown_object_type():
    with pytest.raises(ValueError):
        hubspot_client.get_object("widgets", "1", "token")


def test_get_object_returns_parsed_json(monkeypatch):
    calls = []

    def fake_get(url, headers, params, timeout):
        calls.append((url, headers, params))
        return _FakeResponse({"id": "1246965", "properties": {"name": "Consumers Energy"}})

    monkeypatch.setattr(hubspot_client._session, "get", fake_get)

    result = hubspot_client.get_object("companies", "1246965", "my-token")

    assert result["properties"]["name"] == "Consumers Energy"
    url, headers, params = calls[0]
    assert url == "https://api.hubapi.com/crm/v3/objects/companies/1246965"
    assert headers["Authorization"] == "Bearer my-token"


def test_get_object_requests_explicit_properties_not_hubspot_defaults(monkeypatch):
    # HubSpot's default response omits schema-mapped fields like
    # tt_taiwan_vat_number/company_name_en entirely unless explicitly
    # requested — confirmed by comparing curl-tested default responses
    # against CosmosDB_Table_Schema.md's assumed source properties.
    captured = {}

    def fake_get(url, headers, params, timeout):
        captured["params"] = params
        return _FakeResponse({"id": "1", "properties": {}})

    monkeypatch.setattr(hubspot_client._session, "get", fake_get)

    hubspot_client.get_object("companies", "1", "token")

    requested = captured["params"]["properties"].split(",")
    assert "tt_taiwan_vat_number" in requested
    assert "company_name_en" in requested


def test_iter_objects_follows_pagination_until_no_next_link(monkeypatch):
    pages = [
        {
            "results": [{"id": "1"}, {"id": "2"}],
            "paging": {"next": {"link": "https://api.hubapi.com/crm/v3/objects/companies?after=2"}},
        },
        {"results": [{"id": "3"}]},  # no "paging" key — last page
    ]
    call_urls = []

    def fake_get(url, headers, timeout, params=None):
        call_urls.append(url)
        return _FakeResponse(pages.pop(0))

    monkeypatch.setattr(hubspot_client._session, "get", fake_get)

    results = list(hubspot_client.iter_objects("companies", "my-token"))

    assert [r["id"] for r in results] == ["1", "2", "3"]
    assert call_urls == [
        "https://api.hubapi.com/crm/v3/objects/companies",
        "https://api.hubapi.com/crm/v3/objects/companies?after=2",
    ]


def test_iter_objects_rejects_unknown_object_type():
    with pytest.raises(ValueError):
        list(hubspot_client.iter_objects("widgets", "token"))


def test_iter_objects_first_page_requests_explicit_properties(monkeypatch):
    captured_params = []

    def fake_get(url, headers, timeout, params=None):
        captured_params.append(params)
        return _FakeResponse({"results": []})

    monkeypatch.setattr(hubspot_client._session, "get", fake_get)

    list(hubspot_client.iter_objects("contacts", "token"))

    assert "email" in captured_params[0]["properties"].split(",")


def test_get_associated_company_id_returns_first_result(monkeypatch):
    def fake_get(url, headers, timeout):
        assert url == "https://api.hubapi.com/crm/v4/objects/contacts/999/associations/companies"
        return _FakeResponse({"results": [{"toObjectId": 555, "associationTypes": []}]})

    monkeypatch.setattr(hubspot_client._session, "get", fake_get)

    result = hubspot_client.get_associated_company_id("999", "token")

    assert result == "555"


def test_get_company_business_number_requests_only_vat_properties(monkeypatch):
    captured = {}

    def fake_get(url, headers, params, timeout):
        captured["url"] = url
        captured["properties"] = params["properties"].split(",")
        return _FakeResponse({"properties": {"tt_taiwan_vat_number": "12345678"}})

    monkeypatch.setattr(hubspot_client._session, "get", fake_get)

    result = hubspot_client.get_company_business_number("999", "token")

    assert result == "12345678"
    assert captured["url"] == "https://api.hubapi.com/crm/v3/objects/companies/999"
    assert set(captured["properties"]) == {"tt_taiwan_vat_number", "tt_global_vat_number", "tt_vat_number"}
    assert "name" not in captured["properties"]


def test_get_associated_company_id_returns_none_when_no_association(monkeypatch):
    def fake_get(url, headers, timeout):
        return _FakeResponse({"results": []})

    monkeypatch.setattr(hubspot_client._session, "get", fake_get)

    result = hubspot_client.get_associated_company_id("999", "token")

    assert result is None
