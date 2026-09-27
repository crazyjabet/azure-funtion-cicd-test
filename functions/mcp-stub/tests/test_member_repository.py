import azure.cosmos
import pytest
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from member_repository import (
    CosmosMemberRepository,
    MockMemberRepository,
    get_member_repository,
)


@pytest.fixture(autouse=True)
def _no_real_cosmos_connection(monkeypatch):
    # CosmosClient's constructor makes a real network call (account
    # discovery) even before any query runs. These tests only care about
    # CosmosMemberRepository's own get_member() join logic, not actual
    # Cosmos connectivity, so stub the SDK client out — individual tests
    # that need get_member() to actually do something then monkeypatch
    # repo._container directly (see _patch_containers below).
    monkeypatch.setattr(azure.cosmos, "CosmosClient", lambda endpoint, credential: object())


class _FakeContainer:
    def __init__(self, docs_by_id: dict):
        self._docs_by_id = docs_by_id

    def read_item(self, item, partition_key):
        if item not in self._docs_by_id:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        return self._docs_by_id[item]


def _patch_containers(monkeypatch, repo, credentials: dict, companies: dict):
    containers = {
        repo._credentials_container_name: _FakeContainer(credentials),
        repo._companies_container_name: _FakeContainer(companies),
    }
    monkeypatch.setattr(repo, "_container", lambda name: containers[name])


def test_mock_repository_returns_paid_for_known_member():
    repo = MockMemberRepository()
    member = repo.get_member("known-paid-member@example.com")
    assert member is not None
    assert member.tier == "paid"
    assert member.is_active is True
    assert member.source == "mock"
    assert member.business_number == "mock-business-number"


def test_mock_repository_returns_free_not_none_for_unknown_member():
    repo = MockMemberRepository()
    member = repo.get_member("nobody@example.com")
    assert member is not None
    assert member.tier == "free"
    assert member.source == "mock"
    assert member.business_number is None


def test_get_member_repository_defaults_to_mock(monkeypatch):
    monkeypatch.delenv("MEMBER_REPOSITORY_MODE", raising=False)
    assert isinstance(get_member_repository(), MockMemberRepository)


def _set_cosmos_env(monkeypatch):
    monkeypatch.setenv("COSMOS_DB_ENDPOINT", "https://example-fake-account.documents.azure.com:443/")
    monkeypatch.setenv("COSMOS_DB_DATABASE_NAME", "mcp-membership")
    monkeypatch.setenv("COSMOS_DB_CREDENTIALS_CONTAINER_NAME", "credentials")
    monkeypatch.setenv("COSMOS_DB_COMPANIES_CONTAINER_NAME", "companies")


def test_get_member_repository_returns_cosmos_when_configured(monkeypatch):
    monkeypatch.setenv("MEMBER_REPOSITORY_MODE", "cosmos")
    _set_cosmos_env(monkeypatch)
    assert isinstance(get_member_repository(), CosmosMemberRepository)


def test_cosmos_repository_returns_none_when_email_not_in_credentials(monkeypatch):
    _set_cosmos_env(monkeypatch)
    repo = CosmosMemberRepository()
    _patch_containers(monkeypatch, repo, credentials={}, companies={})

    assert repo.get_member("nobody@example.com") is None


def test_cosmos_repository_joins_credentials_to_companies_for_tier(monkeypatch):
    _set_cosmos_env(monkeypatch)
    repo = CosmosMemberRepository()
    _patch_containers(
        monkeypatch,
        repo,
        credentials={
            "a@b.com": {
                "id": "a@b.com",
                "contact_email": "a@b.com",
                "business_number": "12345678",
                "credential_issued": True,
            }
        },
        companies={"12345678": {"id": "12345678", "business_number": "12345678", "tier": "paid"}},
    )

    member = repo.get_member("a@b.com")

    assert member is not None
    assert member.tier == "paid"
    assert member.is_active is True
    assert member.source == "cosmos"
    assert member.business_number == "12345678"


def test_cosmos_repository_treats_missing_business_number_as_free_tier_no_quota_tracking(monkeypatch):
    _set_cosmos_env(monkeypatch)
    repo = CosmosMemberRepository()
    _patch_containers(
        monkeypatch,
        repo,
        credentials={
            "a@b.com": {
                "id": "a@b.com",
                "contact_email": "a@b.com",
                "business_number": None,
                "credential_issued": False,
            }
        },
        companies={},
    )

    member = repo.get_member("a@b.com")

    assert member is not None
    assert member.tier == "free"
    assert member.is_active is False
    assert member.business_number is None


def test_cosmos_repository_treats_dangling_business_number_reference_as_free_tier(monkeypatch):
    # credentials points at a business_number with no matching company doc
    # (data inconsistency — shouldn't normally happen, but must not crash).
    _set_cosmos_env(monkeypatch)
    repo = CosmosMemberRepository()
    _patch_containers(
        monkeypatch,
        repo,
        credentials={
            "a@b.com": {
                "id": "a@b.com",
                "contact_email": "a@b.com",
                "business_number": "99999999",
                "credential_issued": True,
            }
        },
        companies={},
    )

    member = repo.get_member("a@b.com")

    assert member is not None
    assert member.tier == "free"
    assert member.business_number == "99999999"
