import hubspot_secrets


class _FakeSecret:
    def __init__(self, value):
        self.value = value


class _FakeSecretClient:
    def __init__(self):
        self.calls = 0

    def get_secret(self, name):
        self.calls += 1
        return _FakeSecret(f"token-for-{name}-call-{self.calls}")


def _reset_module_state(monkeypatch):
    monkeypatch.setattr(hubspot_secrets, "_client", None)
    monkeypatch.setattr(hubspot_secrets, "_cached_token", None)


def test_second_call_reuses_cached_token_without_hitting_key_vault_again(monkeypatch):
    _reset_module_state(monkeypatch)
    fake_client = _FakeSecretClient()
    monkeypatch.setattr(hubspot_secrets, "_get_client", lambda: fake_client)
    monkeypatch.setenv("HUBSPOT_PAT_SECRET_NAME", "hubspot-pat-token")

    first = hubspot_secrets.get_hubspot_pat_token()
    second = hubspot_secrets.get_hubspot_pat_token()

    assert first == second
    assert fake_client.calls == 1  # not 2 — the point of the cache


def test_cache_is_process_lifetime_not_reset_between_calls(monkeypatch):
    _reset_module_state(monkeypatch)
    fake_client = _FakeSecretClient()
    monkeypatch.setattr(hubspot_secrets, "_get_client", lambda: fake_client)
    monkeypatch.setenv("HUBSPOT_PAT_SECRET_NAME", "hubspot-pat-token")

    for _ in range(5):
        hubspot_secrets.get_hubspot_pat_token()

    assert fake_client.calls == 1
