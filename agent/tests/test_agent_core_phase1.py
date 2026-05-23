from __future__ import annotations

from app.agent_core import _ensure_default_mailbox_identity
from app.jobs import IdentityRecord
from app.settings import Settings


class _FakeStore:
    def __init__(self, existing: list[IdentityRecord] | None = None) -> None:
        self.records = list(existing or [])
        self.created: list[IdentityRecord] = []

    def list_identities(self, limit: int = 100):
        return self.records[:limit]

    def put_identity(self, record: IdentityRecord) -> IdentityRecord:
        self.records.append(record)
        self.created.append(record)
        return record


def test_ensure_default_mailbox_identity_creates_one_when_missing() -> None:
    settings = Settings()
    store = _FakeStore()
    original_secret = settings.secret
    object.__setattr__(settings, "gmail_account_email_param", "GMAIL_ACCOUNT_EMAIL")
    object.__setattr__(settings, "secret", lambda parameter_name: "friday.nyc.agent@gmail.com" if parameter_name == "GMAIL_ACCOUNT_EMAIL" else "")
    try:
        identity = _ensure_default_mailbox_identity(settings, store)
    finally:
        object.__setattr__(settings, "secret", original_secret)

    assert identity is not None
    assert identity.email == "friday.nyc.agent@gmail.com"
    assert identity.is_default is True
    assert store.created


def test_ensure_default_mailbox_identity_reuses_existing_record() -> None:
    settings = Settings()
    existing = IdentityRecord(
        label="Friday Gmail",
        email="friday.nyc.agent@gmail.com",
        provider="gmail",
        category="shared_mailbox",
        site_scope="shared",
        is_default=True,
    )
    store = _FakeStore(existing=[existing])
    original_secret = settings.secret
    object.__setattr__(settings, "gmail_account_email_param", "GMAIL_ACCOUNT_EMAIL")
    object.__setattr__(settings, "secret", lambda parameter_name: "friday.nyc.agent@gmail.com" if parameter_name == "GMAIL_ACCOUNT_EMAIL" else "")
    try:
        identity = _ensure_default_mailbox_identity(settings, store)
    finally:
        object.__setattr__(settings, "secret", original_secret)

    assert identity == existing
    assert store.created == []
