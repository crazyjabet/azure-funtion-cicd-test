"""
Key Vault secret retrieval for the HubSpot PAT.

2026-09-11 (revised): cached in-process for the lifetime of the worker —
fetched from Key Vault once (whenever this process/instance first needs it),
then reused for every subsequent call without hitting Key Vault again, for
as long as this instance stays warm. User's explicit ask: reduce Key Vault
call volume, not re-fetch on every single invocation.

This still gets a rotated PAT without a redeploy, just not instantly: Flex
Consumption recycles/replaces instances periodically (scale-in, deploys,
platform maintenance), and each fresh instance/cold start re-populates the
cache from Key Vault, picking up whatever is then the latest version. A
rotation takes effect the next time this process restarts, not the next
call.

Auth is DefaultAzureCredential, which resolves to this Function App's
system-assigned managed identity when running in Azure (no key/secret of
its own) — granted `Key Vault Secrets User` on the vault in keyvault.tf.
Locally it falls back to whatever `az login` session is active.
"""

import os

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

_client: SecretClient | None = None
_cached_token: str | None = None


def _get_client() -> SecretClient:
    # Lazy, not module-level, so importing this module doesn't require
    # KEY_VAULT_URI to already be set (matters for unit tests, which
    # monkeypatch get_hubspot_pat_token directly rather than this client).
    global _client
    if _client is None:
        _client = SecretClient(vault_url=os.environ["KEY_VAULT_URI"], credential=DefaultAzureCredential())
    return _client


def get_hubspot_pat_token() -> str:
    global _cached_token
    if _cached_token is None:
        secret_name = os.environ["HUBSPOT_PAT_SECRET_NAME"]
        _cached_token = _get_client().get_secret(secret_name).value
    return _cached_token
