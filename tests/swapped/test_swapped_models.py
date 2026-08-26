"""The app works against scope and identity models it has never seen.

That is the whole claim behind making them swappable, and it is not provable
from the default configuration: ``Meta.swappable`` resolves once per process, so
this file runs under ``tests.settings_swapped`` and its own pytest invocation.

Everything here would pass identically against the shipped models. That is the
point -- the app's behaviour must not depend on which models a project supplied.
"""

from __future__ import annotations

import uuid

import pytest

from tests.swapped.models import Tenant, TenantAuditIdentity, TenantAuditScope
from tests.swapped.repositories import TenantAuditRepository
from vinta_audit_logs.constants import IdentityType, ScopeType
from vinta_audit_logs.models import Audit
from vinta_audit_logs.models_registry import get_audit_identity_model, get_audit_scope_model
from vinta_audit_logs.types import (
    AuditQuery,
    AuditRecordData,
    IdentityRef,
    IdentitySnapshot,
    ScopeRef,
    SubjectRef,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def repository() -> TenantAuditRepository:
    return TenantAuditRepository()


@pytest.fixture
def tenant() -> Tenant:
    return Tenant.objects.create(name="Acme")


def _record(tenant_id: int, **overrides) -> AuditRecordData:
    defaults = {
        "action_key": "create",
        "actor": IdentitySnapshot(
            identity_type=IdentityType.USER,
            identity_key="7",
            metadata={"department": "finance"},
        ),
        "subject": SubjectRef(subject_type="testapp.article", subject_id="1"),
        "scope": ScopeRef(scope_type=ScopeType.SCOPED, scope_key=str(tenant_id)),
    }
    return AuditRecordData(**{**defaults, **overrides})


def test_the_settings_really_did_swap_the_models():
    """Guard the guard: without this the rest of the file proves nothing."""
    assert get_audit_scope_model() is TenantAuditScope
    assert get_audit_identity_model() is TenantAuditIdentity


def test_a_record_writes_against_the_swapped_models(repository, tenant):
    """The unchanged write path, pointed at models with extra columns."""
    stored = repository.add(_record(tenant.pk))

    assert stored.scope.scope_key == str(tenant.pk)
    assert TenantAuditScope.objects.get(scope_key=str(tenant.pk)).tenant_id == tenant.pk


def test_the_scope_hook_fills_the_projects_own_foreign_key(repository, tenant):
    """``build_scope_defaults`` is where the portable key becomes a relation."""
    repository.add(_record(tenant.pk))

    scope = TenantAuditScope.objects.get(scope_key=str(tenant.pk))
    assert scope.tenant == tenant
    assert scope.scope_type == ScopeType.SCOPED


def test_the_identity_hook_fills_the_projects_own_column(repository, tenant):
    """``build_identity_defaults`` reaches a column this app knows nothing about."""
    repository.add(_record(tenant.pk))

    assert TenantAuditIdentity.objects.get().department == "finance"


def test_the_project_column_round_trips_back_into_the_portable_snapshot(repository, tenant):
    """And ``identity_to_snapshot`` puts it back, so a sync carries it."""
    stored = repository.add(_record(tenant.pk))

    assert stored.actor.metadata["department"] == "finance"


def test_portable_filters_still_work_against_swapped_models(repository, tenant):
    """Every ``AuditQuery`` field reads columns on the audit row, not the scope.

    Which is why swapping the scope model cannot change what a filter means.
    """
    other = Tenant.objects.create(name="Other")
    repository.bulk_add(
        [_record(tenant.pk), _record(tenant.pk, action_key="update"), _record(other.pk)]
    )

    assert repository.query(AuditQuery(scope_keys=[str(tenant.pk)])).total == 2
    assert repository.query(AuditQuery(actions=["update"])).total == 1
    assert (
        repository.query(
            AuditQuery(actors=[IdentityRef(identity_type=IdentityType.USER, identity_key="7")])
        ).total
        == 3
    )


def test_the_upsert_contract_holds_against_swapped_models(repository, tenant):
    """Writing the same uid twice still converges on one row."""
    uid = uuid.uuid7()
    repository.add(_record(tenant.pk, uid=uid))
    repository.add(_record(tenant.pk, uid=uid, action_key="delete"))

    assert Audit.objects.filter(uid=uid).count() == 1
    assert Audit.objects.get(uid=uid).action_key == "delete"


def test_the_global_scope_exists_under_a_swapped_model_too(repository):
    """A platform action belongs to no tenant and still needs somewhere to hang."""
    repository.add(
        AuditRecordData(
            action_key="create",
            actor=IdentitySnapshot(identity_type=IdentityType.SYSTEM),
            subject=SubjectRef(subject_type="testapp.article", subject_id="1"),
            scope=ScopeRef.global_scope(),
        )
    )

    scope = TenantAuditScope.objects.get(scope_key="")
    assert scope.tenant_id is None
    assert scope.scope_type == ScopeType.GLOBAL


def test_deleting_a_tenant_is_refused_while_its_audit_scope_exists(repository, tenant):
    """PROTECT on the project's own foreign key, and the log keeps it honest."""
    from django.db.models import ProtectedError

    repository.add(_record(tenant.pk))

    with pytest.raises(ProtectedError):
        tenant.delete()
