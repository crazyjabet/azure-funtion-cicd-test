import hubspot_to_cosmos

_REAL_SHAPED_COMPANY = {
    "id": "48737689691",
    "properties": {
        "name": "Consumers Energy",
        "company_name_en": "Consumers Energy Co.",
        "tt_taiwan_vat_number": "12345678",
        "tt_global_vat_number": None,
        "tt_vat_number": None,
    },
}

_REAL_SHAPED_CONTACT = {
    "id": "241187768981",
    "properties": {
        "email": "guanting.chen@huasheng.example.com",
        "firstname": "冠廷",
        "lastname": "陳",
    },
}


def test_company_business_number_prefers_taiwan_vat():
    company = {
        "id": "1",
        "properties": {
            "tt_taiwan_vat_number": "taiwan",
            "tt_global_vat_number": "global",
            "tt_vat_number": "generic",
        },
    }
    assert hubspot_to_cosmos.company_business_number(company) == "taiwan"


def test_company_business_number_falls_back_through_coalesce_order():
    company = {"id": "1", "properties": {"tt_global_vat_number": "global", "tt_vat_number": "generic"}}
    assert hubspot_to_cosmos.company_business_number(company) == "global"

    company = {"id": "1", "properties": {"tt_vat_number": "generic"}}
    assert hubspot_to_cosmos.company_business_number(company) == "generic"


def test_company_business_number_none_when_all_vat_fields_missing():
    company = {"id": "1", "properties": {}}
    assert hubspot_to_cosmos.company_business_number(company) is None


def test_company_to_cosmos_doc_maps_real_shaped_response():
    doc = hubspot_to_cosmos.company_to_cosmos_doc(_REAL_SHAPED_COMPANY)

    assert doc["id"] == "12345678"
    assert doc["business_number"] == "12345678"
    assert doc["company_name"] == "Consumers Energy"
    assert doc["company_name_en"] == "Consumers Energy Co."
    assert doc["hubspot_company_id"] == 48737689691
    assert doc["tier"] == "free"
    assert doc["monthly_token_quota"] == 0
    assert doc["sync_source"] == "hubspot_sync"
    assert doc["created_at"] == doc["updated_at"]  # first sync — no prior created_at




def test_credential_to_cosmos_doc_maps_real_shaped_response():
    doc = hubspot_to_cosmos.credential_to_cosmos_doc(_REAL_SHAPED_CONTACT, business_number="12345678")

    assert doc["id"] == "guanting.chen@huasheng.example.com"
    assert doc["contact_email"] == "guanting.chen@huasheng.example.com"
    assert doc["business_number"] == "12345678"
    assert doc["hubspot_contact_id"] == 241187768981
    assert doc["contact_name"] == "冠廷陳"
    assert doc["credential_issued"] is False
    assert doc["sync_source"] == "hubspot_sync"


def test_credential_to_cosmos_doc_business_number_none_when_no_association():
    doc = hubspot_to_cosmos.credential_to_cosmos_doc(_REAL_SHAPED_CONTACT, business_number=None)
    assert doc["business_number"] is None
