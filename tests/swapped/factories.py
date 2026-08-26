"""What the swapped settings module points the factory settings at."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.swapped.repositories import TenantAuditRepository
from vinta_audit_logs.services import AuditService

if TYPE_CHECKING:
    from vinta_audit_logs.repositories import AuditRepository


def build_audit_repository() -> AuditRepository:
    return TenantAuditRepository()


def build_audit_service() -> AuditService:
    return AuditService(repository=build_audit_repository())
