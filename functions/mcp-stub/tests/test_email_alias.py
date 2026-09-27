import pytest

from email_alias import alias_email, get_member_with_alias
from member_repository import MemberInfo, MemberRepository


class _FakeRepo(MemberRepository):
    def __init__(self, members: dict[str, MemberInfo]):
        self._members = members
        self.lookups: list[str] = []

    def get_member(self, email: str) -> MemberInfo | None:
        self.lookups.append(email)
        return self._members.get(email)


def _member(email: str) -> MemberInfo:
    return MemberInfo(email=email, tier="paid", is_active=True, source="cosmos", business_number="12345678")


@pytest.fixture
def alias_enabled(monkeypatch):
    monkeypatch.setenv("MEMBER_EMAIL_ALIAS_SOURCE_DOMAIN", "nextlink.com.tw")
    monkeypatch.setenv("MEMBER_EMAIL_ALIAS_TARGET_DOMAIN", "microfusion.cloud")


@pytest.mark.parametrize(
    "email,expected",
    [
        ("jason.hsiao@nextlink.com.tw", "jason.hsiao@microfusion.cloud"),
        ("Jason.Hsiao@NextLink.com.tw", "jason.hsiao@microfusion.cloud"),
        ("someone@example.com", None),
        ("jason.hsiao@evilnextlink.com.tw", None),
        ("jason.hsiao@sub.nextlink.com.tw", None),
        ("jason.hsiao@nextlink.com.tw.evil.com", None),
        ("@nextlink.com.tw", None),
        ("no-at-sign", None),
    ],
)
def test_alias_email(alias_enabled, email, expected):
    assert alias_email(email) == expected


@pytest.mark.parametrize("source,target", [("", ""), ("nextlink.com.tw", ""), ("", "microfusion.cloud")])
def test_alias_disabled_unless_both_domains_set(monkeypatch, source, target):
    monkeypatch.setenv("MEMBER_EMAIL_ALIAS_SOURCE_DOMAIN", source)
    monkeypatch.setenv("MEMBER_EMAIL_ALIAS_TARGET_DOMAIN", target)
    assert alias_email("jason.hsiao@nextlink.com.tw") is None


def test_original_email_wins_over_alias(alias_enabled):
    repo = _FakeRepo({
        "jason.hsiao@nextlink.com.tw": _member("jason.hsiao@nextlink.com.tw"),
        "jason.hsiao@microfusion.cloud": _member("jason.hsiao@microfusion.cloud"),
    })

    member = get_member_with_alias(repo, "jason.hsiao@nextlink.com.tw")

    assert member.email == "jason.hsiao@nextlink.com.tw"
    assert repo.lookups == ["jason.hsiao@nextlink.com.tw"]


def test_falls_back_to_alias_when_original_not_found(alias_enabled):
    repo = _FakeRepo({"jason.hsiao@microfusion.cloud": _member("jason.hsiao@microfusion.cloud")})

    member = get_member_with_alias(repo, "jason.hsiao@nextlink.com.tw")

    assert member.email == "jason.hsiao@microfusion.cloud"
    assert repo.lookups == ["jason.hsiao@nextlink.com.tw", "jason.hsiao@microfusion.cloud"]


def test_alias_not_tried_for_other_domains(alias_enabled):
    repo = _FakeRepo({"someone@microfusion.cloud": _member("someone@microfusion.cloud")})

    assert get_member_with_alias(repo, "someone@example.com") is None
    assert repo.lookups == ["someone@example.com"]


def test_alias_not_tried_when_disabled(monkeypatch):
    monkeypatch.delenv("MEMBER_EMAIL_ALIAS_SOURCE_DOMAIN", raising=False)
    monkeypatch.delenv("MEMBER_EMAIL_ALIAS_TARGET_DOMAIN", raising=False)
    repo = _FakeRepo({"jason.hsiao@microfusion.cloud": _member("jason.hsiao@microfusion.cloud")})

    assert get_member_with_alias(repo, "jason.hsiao@nextlink.com.tw") is None
    assert repo.lookups == ["jason.hsiao@nextlink.com.tw"]
