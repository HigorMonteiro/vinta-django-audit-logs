"""Scope and identity models a project might swap in, with columns of their own.

The suite runs a second time against these to prove the claim the app makes: a
project can point ``AUDIT_SCOPE_MODEL`` and ``AUDIT_IDENTITY_MODEL`` at models
carrying real foreign keys and extra columns, and nothing in the log has to know.
"""

from __future__ import annotations

from django.db import models

from vinta_audit_logs.constants import ScopeType
from vinta_audit_logs.models import AbstractAuditIdentity, AbstractAuditScope


class Tenant(models.Model):
    """The thing this test project scopes its audit log by."""

    name = models.CharField(max_length=255)

    def __str__(self) -> str:
        return self.name


class TenantAuditScope(AbstractAuditScope[Tenant]):
    """A scope that is one tenant, or the whole installation.

    ``PROTECT``, not ``CASCADE``: deleting a tenant must not delete the record of
    what happened inside it.
    """

    tenant = models.ForeignKey(
        Tenant,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="audit_scopes",
    )

    class Meta:
        swappable = "AUDIT_SCOPE_MODEL"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(scope_type=ScopeType.GLOBAL, tenant__isnull=True)
                    | ~models.Q(scope_type=ScopeType.GLOBAL) & models.Q(tenant__isnull=False)
                ),
                name="swapped_scope_type_and_tenant_agree",
            ),
            models.UniqueConstraint(
                fields=["scope_type", "scope_key"],
                name="swapped_scope_unique_key_per_type",
            ),
        ]

    @property
    def scope(self) -> Tenant | None:
        return self.tenant

    @scope.setter
    def scope(self, value: Tenant | None) -> None:
        self.tenant = value
        self.scope_type = ScopeType.GLOBAL if value is None else ScopeType.SCOPED

    @scope.deleter
    def scope(self) -> None:
        self.tenant = None
        self.scope_type = ScopeType.GLOBAL

    def build_scope_key(self) -> str:
        """The tenant's pk as a string; "" for the global scope."""
        return "" if self.tenant_id is None else str(self.tenant_id)


class TenantAuditIdentity(AbstractAuditIdentity):
    """An identity with a column of its own, to prove the hook reaches it."""

    #: Whatever a project wants to filter actors by. Here: the department the
    #: actor belonged to when they acted.
    department = models.CharField(max_length=64, blank=True)

    class Meta(AbstractAuditIdentity.Meta):
        abstract = False
        swappable = "AUDIT_IDENTITY_MODEL"
