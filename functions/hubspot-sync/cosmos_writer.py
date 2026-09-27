"""
Cosmos DB read/upsert for the companies and credentials containers (see
CosmosDB_Table_Schema.md). Auth is DefaultAzureCredential, resolving to this
Function App's system-assigned managed identity when running in Azure — no
account key (see environments/test/cosmosdb.tf's
azurerm_cosmosdb_sql_role_assignment for the grant). This briefly reverted
to a plain COSMOS_DB_KEY app setting when terraform apply's caller couldn't
create RBAC role assignments (a propagation delay, not a real permission
gap — confirmed resolved by directly re-testing role assignment creation);
back to DefaultAzureCredential now that it works.

Each upsert reads the existing document first (if any) and preserves the
fields listed in hubspot_to_cosmos.py's *_FIELDS_PRESERVED_IF_EXISTING —
not just created_at. Those fields (tier, credential_issued, etc.) are never
sourced from HubSpot; hubspot_to_cosmos.py always returns its own defaults
for them since it has no visibility into Cosmos. Without this merge, every
re-sync of the same company/contact (an explicitly expected operation —
manual backfill re-runs, eventually the webhook path) would silently reset
whatever the not-yet-built quota/tier/credential business logic had written
back to those defaults. Found by code review, 2026-09-11.
"""

import functools
import os

from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.identity import DefaultAzureCredential

import hubspot_to_cosmos


@functools.lru_cache(maxsize=1)
def _get_client() -> CosmosClient:
    return CosmosClient(os.environ["COSMOS_DB_ENDPOINT"], credential=DefaultAzureCredential())


def _get_container(container_name_env_var: str):
    database = _get_client().get_database_client(os.environ["COSMOS_DB_DATABASE_NAME"])
    return database.get_container_client(os.environ[container_name_env_var])


def _read_existing(container, doc_id: str, partition_key: str) -> dict | None:
    try:
        return container.read_item(item=doc_id, partition_key=partition_key)
    except CosmosResourceNotFoundError:
        return None


def _upsert(container_name_env_var: str, doc: dict, partition_key_field: str, preserved_fields: tuple) -> None:
    container = _get_container(container_name_env_var)
    existing = _read_existing(container, doc["id"], doc[partition_key_field])
    if existing is not None:
        for field in preserved_fields:
            doc[field] = existing[field]
    container.upsert_item(doc)


def upsert_company(doc: dict) -> None:
    """doc must already have business_number populated (both as `id` and
    `business_number`, the partition key) — callers check this before
    calling, since a missing business_number can't be written at all (see
    hubspot_to_cosmos.py's docstring)."""
    _upsert(
        "COSMOS_DB_COMPANIES_CONTAINER_NAME",
        doc,
        partition_key_field="business_number",
        preserved_fields=hubspot_to_cosmos.COMPANY_FIELDS_PRESERVED_IF_EXISTING,
    )


def upsert_credential(doc: dict) -> None:
    """doc must already have contact_email populated (both as `id` and
    `contact_email`, the partition key)."""
    _upsert(
        "COSMOS_DB_CREDENTIALS_CONTAINER_NAME",
        doc,
        partition_key_field="contact_email",
        preserved_fields=hubspot_to_cosmos.CREDENTIAL_FIELDS_PRESERVED_IF_EXISTING,
    )
