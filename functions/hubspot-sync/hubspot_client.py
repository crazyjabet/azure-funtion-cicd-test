"""
Minimal HubSpot CRM API client — read-only (list + single-object lookup +
one association lookup), used by function_app.py's queue_worker. This
project only reads from HubSpot so far; nothing here writes back to
HubSpot's own data.

Endpoints match what was manually curl-tested against the sandbox portal
earlier in this session (see hubspot_api_connectivity_test.md) — companies,
contacts, deals under /crm/v3/objects/{type}, associations under
/crm/v4/objects/{type}/{id}/associations/{toType}.

IMPORTANT (found 2026-09-11 by comparing the curl-tested responses against
CosmosDB_Table_Schema.md's assumed HubSpot source properties): HubSpot's
list/get endpoints only return a small DEFAULT property set unless you pass
`properties=...` explicitly — our earlier curl tests only ever saw
name/domain/hs_object_id-type defaults, never the schema's actual source
fields (tt_taiwan_vat_number, company_name_en, email is the exception since
it happens to be a default contact property, hs_is_closed_won, etc.).
`_PROPERTIES_BY_OBJECT_TYPE` below is what makes those fields actually show
up in the response.
"""

import requests

_BASE_URL = "https://api.hubapi.com"
_TIMEOUT_SECONDS = 15

OBJECT_TYPES = {"companies", "contacts", "deals"}

# Explicit properties to request per object type — see module docstring for
# why this is necessary at all. Only lists what CosmosDB_Table_Schema.md
# actually maps into a Cosmos field; add to this list (not just assume it's
# already in the response) if a new mapped field is added there.
_PROPERTIES_BY_OBJECT_TYPE = {
    "companies": [
        "name",
        "company_name_en",
        "tt_taiwan_vat_number",
        "tt_global_vat_number",
        "tt_vat_number",
    ],
    "contacts": ["email", "firstname", "lastname"],
    "deals": ["dealname", "closedate", "hs_is_closed_won"],
}

# Just the 3 VAT properties — used when a caller (the credentials sync path)
# only needs business_number from a company, not the full property set
# above. Found by code review: _store_credential was calling get_object and
# fetching name/company_name_en it never uses, once per contact.
_BUSINESS_NUMBER_PROPERTIES = ["tt_taiwan_vat_number", "tt_global_vat_number", "tt_vat_number"]

# Shared across calls (module-level, one per worker process) so
# iter_objects's pagination loop reuses one keep-alive connection instead of
# a fresh TCP/TLS handshake per page — matters for the "possibly long" bulk
# fetch path the Storage Queue design exists to accommodate.
_session = requests.Session()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _require_known_object_type(object_type: str) -> None:
    if object_type not in OBJECT_TYPES:
        raise ValueError(f"unsupported object_type: {object_type!r}, expected one of {sorted(OBJECT_TYPES)}")


def get_object(object_type: str, object_id: str, token: str) -> dict:
    _require_known_object_type(object_type)
    url = f"{_BASE_URL}/crm/v3/objects/{object_type}/{object_id}"
    params = {"properties": ",".join(_PROPERTIES_BY_OBJECT_TYPE[object_type])}
    resp = _session.get(url, headers=_headers(token), params=params, timeout=_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def iter_objects(object_type: str, token: str, page_size: int = 100):
    """Yields every object of the given type, paging via HubSpot's
    paging.next.link until exhausted."""
    _require_known_object_type(object_type)
    url = f"{_BASE_URL}/crm/v3/objects/{object_type}"
    params = {"limit": page_size, "properties": ",".join(_PROPERTIES_BY_OBJECT_TYPE[object_type])}
    while url:
        resp = _session.get(url, headers=_headers(token), params=params, timeout=_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        yield from data.get("results", [])
        # HubSpot's "next" link is a fully-formed URL that already repeats
        # the original query string (including properties=...), so it
        # doesn't need to be re-added here.
        url = data.get("paging", {}).get("next", {}).get("link")
        params = None


def get_company_business_number(company_id: str, token: str) -> str | None:
    """Like get_object("companies", company_id, token) followed by
    hubspot_to_cosmos.company_business_number, but only requests the 3 VAT
    properties actually needed for that — not the full companies property
    set (name, company_name_en, ...) that caller never uses. Used by the
    credentials sync path, which calls this once per contact."""
    url = f"{_BASE_URL}/crm/v3/objects/companies/{company_id}"
    params = {"properties": ",".join(_BUSINESS_NUMBER_PROPERTIES)}
    resp = _session.get(url, headers=_headers(token), params=params, timeout=_TIMEOUT_SECONDS)
    resp.raise_for_status()
    props = resp.json().get("properties", {})
    # Same COALESCE order as hubspot_to_cosmos.company_business_number —
    # duplicated rather than imported from there, since this is the lower
    # layer (hubspot_to_cosmos depends on this module's shapes already;
    # the reverse would be a layering cycle for 3 lines of logic).
    return props.get("tt_taiwan_vat_number") or props.get("tt_global_vat_number") or props.get("tt_vat_number")


def get_associated_company_id(contact_id: str, token: str) -> str | None:
    """Looks up the HubSpot company associated with a contact (reverse
    direction of the companies->contacts association curl-tested earlier —
    see hubspot_api_connectivity_test.md). Returns the company's HubSpot
    object ID, or None if the contact has no associated company.

    Needed because credentials.business_number isn't a Contact property —
    it belongs to the associated Company record (CosmosDB_Table_Schema.md
    §4 item 4: "這是HubSpot的Association機制，不是Property"). Only the
    first association is used; a contact having multiple associated
    companies isn't a case the schema doc accounts for.
    """
    url = f"{_BASE_URL}/crm/v4/objects/contacts/{contact_id}/associations/companies"
    resp = _session.get(url, headers=_headers(token), timeout=_TIMEOUT_SECONDS)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        return None
    return str(results[0]["toObjectId"])
