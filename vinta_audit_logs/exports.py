"""The app's public surface, gathered in one importable place.

Deliberately not ``__init__.py``: this app's modules import Django models, and a
package ``__init__`` runs while the app registry is still populating. Import from
here (``from vinta_audit_logs.exports import AuditQuery``) or from the module
that defines the name.
"""

from vinta_audit_logs.constants import AuditActionKey, IdentityType, ScopeType
from vinta_audit_logs.diff import compute_diff
from vinta_audit_logs.exceptions import AuditError, UnknownAuditRepositoryError
from vinta_audit_logs.filtering import apply_query, normalize_ordering, record_matches
from vinta_audit_logs.models_registry import get_audit_identity_model, get_audit_scope_model
from vinta_audit_logs.repositories import (
    AuditRepository,
    DjangoORMAuditRepository,
    InMemoryAuditRepository,
)
from vinta_audit_logs.services import AuditService
from vinta_audit_logs.types import (
    AuditPage,
    AuditQuery,
    AuditRecord,
    AuditRecordData,
    AuditSyncResult,
    IdentityRef,
    IdentitySnapshot,
    ScopeKey,
    ScopeRef,
    SubjectKey,
    SubjectRef,
)

__all__ = [
    "AuditActionKey",
    "AuditError",
    "AuditPage",
    "AuditQuery",
    "AuditRecord",
    "AuditRecordData",
    "AuditRepository",
    "AuditService",
    "AuditSyncResult",
    "DjangoORMAuditRepository",
    "IdentityRef",
    "IdentitySnapshot",
    "IdentityType",
    "InMemoryAuditRepository",
    "ScopeKey",
    "ScopeRef",
    "ScopeType",
    "SubjectKey",
    "SubjectRef",
    "UnknownAuditRepositoryError",
    "apply_query",
    "compute_diff",
    "get_audit_identity_model",
    "get_audit_scope_model",
    "normalize_ordering",
    "record_matches",
]
