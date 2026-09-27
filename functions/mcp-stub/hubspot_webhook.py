"""
HubSpot Workflow "Custom Webhook Action" receiver (Enterprise Workflows).

2026-09-11: primary authentication is APIM's check-header policy
(apim_hubspot_webhook.tf), reading a fixed self-issued API key from Key
Vault via a named value — matching this repo's existing pattern of doing
auth in APIM (validate-azure-ad-token for the OAuth path).

`_is_authorized` below is a SECOND, defense-in-depth check of the same key,
not the primary gate — added after code review flagged that this Function
App has no network restriction locking it to APIM-only traffic, so its own
*.azurewebsites.net hostname is directly, publicly POST-able, completely
bypassing APIM if someone finds it. `/mcp/*` in function_app.py already
re-checks its own auth for the identical reason (see that route's comment);
this mirrors it. Fetched via a Key Vault reference app setting (resolved by
App Service itself, refreshed roughly every 24h), not the SDK — this key
doesn't need instant-rotation semantics the way the HubSpot PAT does
(hubspot_secrets.py, functions/hubspot-sync/), just to not be hardcoded.

Why not the CRM Webhooks Subscription API's native v3 HMAC signature
instead: see CLAUDE.md's 2026-09-11 decision note (no HubSpot portal access
to retrieve the Private App's Client Secret, and HubSpot doesn't support
OAuth for either webhook mechanism).

HubSpot Workflow custom webhook actions send a flat JSON body of only the
properties the workflow author chose to include — no nested fields, not a
batch array like CRM Webhooks. 2026-09-11: the customer (貿協) confirmed the
Workflow is built on Company (member = company, not contact) and configured
to send exactly one field, `hs_object_id` — see
docs/guides/HubSpot_Webhook_Customer_Setup_Guide.md for the customer-facing
setup instructions this matches. That decision is what unblocks the relay
below; before it, this handler only logged the payload with no assumptions
about its shape.

On a valid `hs_object_id`, this relays to the hubspot-sync Function App's
`http_trigger` (functions/hubspot-sync/function_app.py) to actually fetch
the full company record and write it to Cosmos DB — this handler itself
still does no HubSpot API calls and no Cosmos DB writes, staying fast and
simple; hubspot-sync already has the queue-based design for that (see its
own module docstring for why). The relay call is to hubspot-sync's
http_trigger, which just enqueues and returns — fast, so doing it
synchronously here doesn't risk this handler's own response time. Best
effort: a relay failure is logged but doesn't fail this response, since
HubSpot already got what it needed (a 200 acknowledging the notification
itself) — retrying the relay is hubspot-sync's/an operator's job (manual
catch-up via http_trigger directly), not something worth making HubSpot
retry the whole webhook for.
"""

import hmac
import json
import logging
import os

import azure.functions as func
import requests

bp = func.Blueprint()

_API_KEY = os.environ.get("HUBSPOT_WEBHOOK_API_KEY", "")
_HUBSPOT_SYNC_TRIGGER_URL = os.environ.get("HUBSPOT_SYNC_TRIGGER_URL", "")
_HUBSPOT_SYNC_FUNCTION_KEY = os.environ.get("HUBSPOT_SYNC_FUNCTION_KEY", "")
_RELAY_TIMEOUT_SECONDS = 10


def _is_authorized(req: func.HttpRequest) -> bool:
    if not _API_KEY:
        return False
    provided = req.headers.get("Authorization", "")
    return hmac.compare_digest(provided, f"Bearer {_API_KEY}")


def _relay_to_hubspot_sync(hs_object_id: str) -> None:
    if not _HUBSPOT_SYNC_TRIGGER_URL:
        logging.warning("hubspot_webhook: HUBSPOT_SYNC_TRIGGER_URL not configured, skipping relay")
        return

    try:
        resp = requests.post(
            _HUBSPOT_SYNC_TRIGGER_URL,
            params={"code": _HUBSPOT_SYNC_FUNCTION_KEY},
            json={"object_type": "companies", "object_id": hs_object_id},
            timeout=_RELAY_TIMEOUT_SECONDS,
        )
        if resp.status_code >= 400:
            logging.warning(
                "hubspot_webhook: relay to hubspot-sync failed, status=%d body=%s", resp.status_code, resp.text
            )
        else:
            logging.info(
                "hubspot_webhook: relayed hs_object_id=%s to hubspot-sync (status=%d)",
                hs_object_id,
                resp.status_code,
            )
    except requests.RequestException as e:
        # Best effort — see module docstring. A failed relay doesn't fail
        # this response; HubSpot already got its 200.
        logging.warning("hubspot_webhook: relay to hubspot-sync raised %s", e)


@bp.route(route="hubspot/webhook", methods=["POST"])
def hubspot_webhook(req: func.HttpRequest) -> func.HttpResponse:
    if not _is_authorized(req):
        # Normally means either APIM's check-header policy is misconfigured
        # (shouldn't happen — it's the primary gate) or this Function App
        # got called directly, bypassing APIM entirely.
        logging.warning("hubspot_webhook: defense-in-depth check failed (missing/incorrect Authorization header)")
        return func.HttpResponse(
            json.dumps({"error": "unauthorized"}), status_code=401, mimetype="application/json"
        )

    try:
        payload = req.get_json()
    except ValueError:
        logging.warning("hubspot_webhook: body is not JSON")
        return func.HttpResponse(
            json.dumps({"error": "invalid_body"}), status_code=400, mimetype="application/json"
        )

    logging.info("hubspot_webhook: payload=%s", payload)

    hs_object_id = payload.get("hs_object_id") if isinstance(payload, dict) else None
    if hs_object_id:
        _relay_to_hubspot_sync(str(hs_object_id))
    else:
        # Not an error — e.g. the Workflow's body config gets changed later,
        # or someone hits this endpoint manually while testing. Still 200:
        # the notification itself was received and logged fine.
        logging.warning("hubspot_webhook: no hs_object_id in payload, skipping hubspot-sync relay")

    return func.HttpResponse(status_code=200)
