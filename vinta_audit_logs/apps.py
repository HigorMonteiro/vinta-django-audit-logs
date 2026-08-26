from __future__ import annotations

from django.apps import AppConfig

from vinta_audit_logs import conf

# Before any model in this app is imported -- see the docstring for why that
# matters, and why this is not in ``ready()``.
conf.install_swappable_defaults()


class VintaAuditLogsConfig(AppConfig):
    name = "vinta_audit_logs"
    verbose_name = "Audit logs"
