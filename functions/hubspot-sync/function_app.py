"""
hubspot-sync Function App — calls HubSpot's own CRM API (read-only), using
the PAT from Key Vault (hubspot_secrets.py). Separate Function App from
mcp-stub (Flex Consumption, not Y1) — see
../../terraform/modules/functions-flex/main.tf for why.

Covers three scenarios (CLAUDE.md's 2026-09-11 decision note):
1. Manual initial backfill after go-live (human calls http_trigger).
2. Manual catch-up if the webhook path (mcp-stub's hubspot_webhook.py)
   breaks (same http_trigger).
3. Webhook-triggered single-record fetch — NOT wired up yet, since it
   depends on which field the not-yet-built HubSpot Workflow puts the
   record's HubSpot object ID under. http_trigger already supports the
   single-record shape (object_id) that scenario would need; only the
   automatic call from hubspot_webhook.py is missing.

http_trigger only enqueues and returns 202 — it never calls HubSpot inline.
HTTP-triggered functions are hard-capped at 230s response time by the
platform regardless of hosting plan (Azure Load Balancer's default idle
timeout), so a bulk fetch that might run long can't happen in the
request/response cycle. queue_worker, not being HTTP-triggered, isn't
subject to that cap — host.json's functionTimeout (30 min) applies there.

2026-09-11: writes companies and contacts to Cosmos DB (companies /
credentials containers, CosmosDB_Table_Schema.md), replacing the earlier
"log only" placeholder now that a real schema exists. Deals still aren't
written anywhere — the schema only uses Deal data to populate
companies.deal_id/deal_closed_at, and that's explicitly deferred (see
hubspot_to_cosmos.py's docstring for why); deals stay fetchable for
testing/inspection, just not persisted.
"""

import json
import logging
import os

import azure.functions as func

import cosmos_writer
import hubspot_client
import hubspot_secrets
import hubspot_to_cosmos

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

_QUEUE_NAME = os.environ.get("HUBSPOT_SYNC_QUEUE_NAME", "hubspot-sync-jobs")


@app.route(route="trigger", methods=["POST"])
@app.queue_output(arg_name="job_out", queue_name=_QUEUE_NAME, connection="AzureWebJobsStorage")
def http_trigger(req: func.HttpRequest, job_out: func.Out[str]) -> func.HttpResponse:
    """
    Body: {"object_type": "companies"|"contacts"|"deals", "object_id": "<optional>"}
    Omitting object_id enqueues a full bulk fetch of that object type;
    including it enqueues a single-record fetch.
    """
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "invalid_body"}), status_code=400, mimetype="application/json"
        )

    object_type = body.get("object_type") if isinstance(body, dict) else None
    if object_type not in hubspot_client.OBJECT_TYPES:
        return func.HttpResponse(
            json.dumps({"error": "invalid_object_type", "allowed": sorted(hubspot_client.OBJECT_TYPES)}),
            status_code=400,
            mimetype="application/json",
        )

    object_id = body.get("object_id")
    if object_id is not None and object_id == "":
        # Reject explicitly rather than letting queue_worker's `if object_id`
        # silently treat "" the same as "omitted" — that would turn what
        # looks like a targeted single-record request into an unintended
        # full bulk fetch of every object of that type.
        return func.HttpResponse(
            json.dumps({"error": "empty_object_id"}), status_code=400, mimetype="application/json"
        )

    job = {"object_type": object_type, "object_id": object_id}
    job_out.set(json.dumps(job))

    logging.info("http_trigger: enqueued job=%s", job)
    return func.HttpResponse(
        json.dumps({"status": "queued", "job": job}), status_code=202, mimetype="application/json"
    )


def _store_company(record: dict) -> None:
    business_number = hubspot_to_cosmos.company_business_number(record)
    if not business_number:
        # Real, documented possibility — schema doc §4 item 2: the three
        # source VAT properties are filled on under 50% of records. Skip
        # rather than write a document with a missing partition key.
        logging.warning(
            "queue_worker: company hubspot_id=%s has no VAT-derived business_number, skipping Cosmos write",
            record.get("id"),
        )
        return
    cosmos_writer.upsert_company(hubspot_to_cosmos.company_to_cosmos_doc(record))


def _store_credential(record: dict, token: str, business_number_cache: dict) -> None:
    props = record.get("properties", {})
    if not props.get("email"):
        # schema doc: Contact.email fill rate 97.76%, not 100%.
        logging.warning(
            "queue_worker: contact hubspot_id=%s has no email, skipping Cosmos write", record.get("id")
        )
        return

    business_number = None
    company_id = hubspot_client.get_associated_company_id(record["id"], token)
    if company_id is not None:
        # Memoized per queue_worker invocation — a bulk contacts backfill
        # commonly has many contacts per company, and without this every
        # single contact re-fetches the same company from HubSpot (found by
        # code review: e.g. 500 contacts across 50 companies meant 500 API
        # calls instead of 50).
        if company_id not in business_number_cache:
            business_number_cache[company_id] = hubspot_client.get_company_business_number(company_id, token)
        business_number = business_number_cache[company_id]

    cosmos_writer.upsert_credential(hubspot_to_cosmos.credential_to_cosmos_doc(record, business_number))


def _store_record(object_type: str, record: dict, token: str, business_number_cache: dict) -> None:
    if object_type == "companies":
        _store_company(record)
    elif object_type == "contacts":
        _store_credential(record, token, business_number_cache)
    # "deals": intentionally not persisted — see module docstring.


@app.queue_trigger(arg_name="msg", queue_name=_QUEUE_NAME, connection="AzureWebJobsStorage")
def queue_worker(msg: func.QueueMessage) -> None:
    raw_body = msg.get_body().decode("utf-8")
    job = json.loads(raw_body)

    # http_trigger always sends a well-formed job, but queue messages can
    # also arrive from a manual replay/edit (e.g. via Storage Explorer
    # during an incident) or a future second producer — validate explicitly
    # rather than letting a malformed message surface as a bare, unlogged
    # KeyError that's hard to diagnose once it lands in the poison queue.
    # isinstance check first: valid JSON that isn't an object (e.g. "[]",
    # "123") would otherwise raise an unguarded, unlogged AttributeError on
    # the .get() call below — a different, less diagnosable failure than
    # the ValueError this whole block exists to produce instead.
    if not isinstance(job, dict):
        logging.error("queue_worker: malformed job, body is not a JSON object: %s", raw_body)
        raise ValueError(f"malformed hubspot-sync job: {raw_body!r}")

    object_type = job.get("object_type")
    if object_type not in hubspot_client.OBJECT_TYPES:
        logging.error("queue_worker: malformed job, bad/missing object_type: %s", raw_body)
        raise ValueError(f"malformed hubspot-sync job: {raw_body!r}")

    # `is not None`, not plain truthiness — an explicit "" would otherwise
    # be treated the same as "omitted" and silently trigger a full bulk
    # fetch (http_trigger already rejects "" before enqueueing, but this
    # guards the same way against a manually-edited/replayed message).
    object_id = job.get("object_id")

    token = hubspot_secrets.get_hubspot_pat_token()
    # Scoped to this one invocation, not module-level — a single job's
    # worth of contact->company lookups share it, but it doesn't leak
    # memory or go stale across unrelated jobs on a warm instance.
    business_number_cache: dict = {}

    if object_id is not None:
        record = hubspot_client.get_object(object_type, object_id, token)
        _store_record(object_type, record, token, business_number_cache)
        logging.info("queue_worker: fetched+stored single %s/%s: id=%s", object_type, object_id, record.get("id"))
        return

    count = 0
    try:
        for record in hubspot_client.iter_objects(object_type, token):
            count += 1
            _store_record(object_type, record, token, business_number_cache)
            logging.info("queue_worker: fetched+stored %s #%d: id=%s", object_type, count, record.get("id"))
    except Exception:
        # No pagination checkpoint — a failure here means a retry (Azure
        # redelivers the same queue message up to host.json's default
        # dequeue count) re-fetches everything from page 1, amplifying
        # calls against HubSpot's rate-limited API. Logging exactly where
        # it broke at least makes that retry cost visible instead of
        # silent; a real checkpoint/resume mechanism is more than this PoC
        # needs right now.
        logging.error("queue_worker: bulk fetch of %s failed after %d record(s)", object_type, count)
        raise

    logging.info("queue_worker: bulk fetch of %s complete, %d record(s)", object_type, count)
