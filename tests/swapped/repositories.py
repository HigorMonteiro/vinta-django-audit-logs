"""The repository, taught the columns :mod:`tests.swapped.models` adds.

The worked example of the write-side hook: a project overrides two methods and
inherits the upsert, the batching, the filter translation and the streaming read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vinta_audit_logs.repositories import DjangoORMAuditRepository
from vinta_audit_logs.types import IdentitySnapshot, ScopeRef

if TYPE_CHECKING:
    from django.db.models import Model


class TenantAuditRepository(DjangoORMAuditRepository):
    """Maps portable audit DTOs onto the swapped scope and identity models."""

    def build_scope_defaults(self, ref: ScopeRef) -> dict[str, Any]:
        """Turn the portable scope key back into a tenant foreign key."""
        tenant_id: int | None = None
        if ref.scope_key:
            try:
                tenant_id = int(ref.scope_key)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Audit scope key {ref.scope_key!r} is not a tenant id.") from exc
        return {"tenant_id": tenant_id, "label": ref.label}

    def build_identity_defaults(self, snapshot: IdentitySnapshot) -> dict[str, Any]:
        """Lift the project's own field out of the portable metadata."""
        defaults = super().build_identity_defaults(snapshot)
        defaults["department"] = (snapshot.metadata or {}).get("department", "")
        return defaults

    def identity_to_snapshot(self, identity: Model) -> IdentitySnapshot:
        """And put it back, so a record round trips unchanged."""
        snapshot = super().identity_to_snapshot(identity)
        metadata = dict(snapshot.metadata)
        if identity.department:
            metadata["department"] = identity.department
        return IdentitySnapshot(
            identity_type=snapshot.identity_type,
            identity_key=snapshot.identity_key,
            identity_label=snapshot.identity_label,
            user_id=snapshot.user_id,
            is_staff=snapshot.is_staff,
            is_superuser=snapshot.is_superuser,
            group_names=snapshot.group_names,
            permission_keys=snapshot.permission_keys,
            metadata=metadata,
        )
