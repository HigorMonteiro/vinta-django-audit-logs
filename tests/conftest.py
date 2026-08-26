"""Shared fixtures.

``scope_ref`` and friends are helpers rather than fixtures where they take
arguments: a test that needs three records in two scopes reads better building
them inline than threading fixtures through.
"""

from __future__ import annotations

import pytest

from vinta_audit_logs.constants import IdentityType, ScopeType
from vinta_audit_logs.repositories import DjangoORMAuditRepository, InMemoryAuditRepository
from vinta_audit_logs.services import AuditService
from vinta_audit_logs.types import (
    AuditRecordData,
    IdentitySnapshot,
    ScopeRef,
    SubjectRef,
)


@pytest.fixture
def repository(db) -> DjangoORMAuditRepository:
    """The ORM repository, against whichever models are configured."""
    return DjangoORMAuditRepository()


@pytest.fixture
def memory_repository() -> InMemoryAuditRepository:
    """The second implementation of the interface."""
    return InMemoryAuditRepository()


@pytest.fixture
def service(repository) -> AuditService:
    """A service writing to the ORM repository and nowhere else."""
    return AuditService(repository=repository)


def scoped(scope_key: str, label: str = "") -> ScopeRef:
    """A scope reference for one tenant."""
    return ScopeRef(scope_type=ScopeType.SCOPED, scope_key=scope_key, label=label)


def record_data(scope_key: str = "1", **overrides) -> AuditRecordData:
    """A record with sensible defaults, scoped to ``scope_key``."""
    defaults = {
        "action_key": "create",
        "actor": IdentitySnapshot(
            identity_type=IdentityType.USER,
            identity_key="7",
            identity_label="someone",
        ),
        "subject": SubjectRef(subject_type="testapp.article", subject_id="1"),
        "scope": scoped(scope_key),
    }
    return AuditRecordData(**{**defaults, **overrides})
