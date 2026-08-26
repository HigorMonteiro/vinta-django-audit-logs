"""Settings for the swapped-model run.

``Meta.swappable`` resolves once, when the models are imported, so a project
cannot swap the scope or identity model halfway through a process. That is why
this is its own settings module and its own pytest invocation rather than an
``override_settings``.

The point of the run is that the app works against models it has never seen:
``tests.swapped`` adds columns to both, and the whole suite has to keep passing.
"""

from __future__ import annotations

from tests.settings import *  # noqa: F403

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "vinta_audit_logs",
    "tests.testapp",
    "tests.swapped",
]

AUDIT_SCOPE_MODEL = "swapped.TenantAuditScope"
AUDIT_IDENTITY_MODEL = "swapped.TenantAuditIdentity"
AUDIT_SERVICE_FACTORY = "tests.swapped.factories.build_audit_service"
AUDIT_REPOSITORY_FACTORY = "tests.swapped.factories.build_audit_repository"
