"""Factories for the audit log models, for use in tests.

Scope is always explicit -- never defaulted -- so a test that forgets which
scope a record belongs to fails at creation rather than quietly writing into the
global one.
"""

from vinta_audit_logs.constants import AuditActionKey, IdentityType, ScopeType
from vinta_audit_logs.models import Audit, AuditAction, AuditAffectedIdentity
from vinta_audit_logs.models_registry import get_audit_identity_model, get_audit_scope_model


class AuditScopeFactory:
    """Creates scope rows through whichever model ``AUDIT_SCOPE_MODEL`` names."""

    def create(self, scope_key: str, **overrides):
        """Create a scoped (non-global) scope row keyed on ``scope_key``."""
        model = get_audit_scope_model()
        defaults = {
            "scope_type": ScopeType.SCOPED,
            "scope_key": scope_key,
            "label": scope_key,
        }
        defaults.update(overrides)
        instance = model(**defaults)
        instance.save()
        return instance

    def create_global(self, **overrides):
        """Create the global scope row."""
        model = get_audit_scope_model()
        defaults = {"scope_type": ScopeType.GLOBAL, "scope_key": "", "label": "Global"}
        defaults.update(overrides)
        instance = model(**defaults)
        instance.save()
        return instance


class AuditIdentityFactory:
    """Creates identity rows through whichever model ``AUDIT_IDENTITY_MODEL`` names."""

    def create(self, **overrides):
        """Create one identity row; defaults to the system actor."""
        model = get_audit_identity_model()
        defaults = {
            "identity_type": IdentityType.SYSTEM,
            "identity_key": "",
            "identity_label": "system",
        }
        defaults.update(overrides)
        return model.objects.create(**defaults)


class AuditActionFactory:
    """Creates action rows."""

    def create(self, key: str = AuditActionKey.CREATE, **overrides) -> AuditAction:
        """Find or create the action row for ``key``."""
        defaults = {"name": str(key), "content_type_key": ""}
        defaults.update(overrides)
        content_type_key = defaults.pop("content_type_key")
        action, _created = AuditAction.objects.get_or_create(
            key=str(key), content_type_key=content_type_key, defaults=defaults
        )
        return action


class AuditFactory:
    """Creates Audit rows, wiring up the dimension rows they require."""

    def __init__(self) -> None:
        self.scopes = AuditScopeFactory()
        self.identities = AuditIdentityFactory()
        self.actions = AuditActionFactory()

    def create(self, scope, **overrides) -> Audit:
        """Create and persist one Audit row in ``scope``.

        Args:
            scope: The scope row this record belongs to.
            **overrides: Any Audit field values to override the defaults.

        Returns:
            A persisted Audit instance.
        """
        action = overrides.pop("action", None) or self.actions.create()
        actor = overrides.pop("actor", None) or self.identities.create()
        defaults: dict = {
            "action": action,
            "action_key": action.key,
            "scope": scope,
            "scope_type": scope.scope_type,
            "scope_key": scope.scope_key,
            "actor": actor,
            "subject_content_type_key": "vinta_audit_logs.audit",
            "subject_pk": "1",
        }
        defaults.update(overrides)
        return Audit.objects.create(**defaults)

    def add_affected(self, audit: Audit, identity) -> AuditAffectedIdentity:
        """Link an identity to an audit record as an affected party."""
        return AuditAffectedIdentity.objects.create(audit=audit, identity=identity)
