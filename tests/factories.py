"""What ``AUDIT_SERVICE_FACTORY`` and ``AUDIT_REPOSITORY_FACTORY`` point at.

A real project resolves these from wherever it keeps its dependencies -- a DI
container, a module-level singleton, a settings-driven registry. The test
project just builds them.
"""

from __future__ import annotations

from vinta_audit_logs.repositories import AuditRepository, DjangoORMAuditRepository
from vinta_audit_logs.services import AuditService


def build_audit_repository() -> AuditRepository:
    """The ORM repository, against the models this app ships."""
    return DjangoORMAuditRepository()


def build_audit_service() -> AuditService:
    """A service writing to the ORM repository and nowhere else."""
    return AuditService(repository=build_audit_repository())
