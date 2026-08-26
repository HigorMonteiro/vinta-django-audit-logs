"""Every setting this app reads, in one place, with its default.

Django settings are a flat global namespace, so an installable app has to be a
good citizen in it: prefix everything, default everything that can be defaulted,
and fail with a sentence rather than an ``AttributeError`` for the ones that
cannot.

The two model settings have no default that would be right for a project that
has overridden them, so :func:`require` is used for those. Everything else falls
back to something that works out of the box.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

#: The scope model, as ``"app_label.ModelName"``. Defaults to the one this app
#: ships; a project with a real tenant boundary points it at its own.
AUDIT_SCOPE_MODEL = "AUDIT_SCOPE_MODEL"
DEFAULT_SCOPE_MODEL = "vinta_audit_logs.AuditScope"

#: The identity model, same shape and same reasoning.
AUDIT_IDENTITY_MODEL = "AUDIT_IDENTITY_MODEL"
DEFAULT_IDENTITY_MODEL = "vinta_audit_logs.AuditIdentity"

#: Dotted path to the callable that arranges for a record to be persisted.
#: Defaults to the Celery task, because a queue is where an audit write belongs;
#: a project without one points this at ``dispatch_inline``.
AUDIT_RECORD_DISPATCHER = "AUDIT_RECORD_DISPATCHER"
DEFAULT_RECORD_DISPATCHER = "vinta_audit_logs.dispatch.dispatch_via_celery"

#: Dotted path to the project's Celery application. Only read by the Celery
#: dispatcher, so a project using another queue never needs it.
AUDIT_CELERY_APP = "AUDIT_CELERY_APP"

#: Dotted path to a zero-argument callable returning a configured
#: ``AuditService``. Read in the worker, where there is no request to carry one.
AUDIT_SERVICE_FACTORY = "AUDIT_SERVICE_FACTORY"

#: Dotted path to a zero-argument callable returning an ``AuditRepository``.
#: Read by the admin, which reads the log through whatever backend holds it.
AUDIT_REPOSITORY_FACTORY = "AUDIT_REPOSITORY_FACTORY"


def get(name: str, default: Any = None) -> Any:
    """Read one setting, falling back to ``default``."""
    return getattr(settings, name, default)


def require(name: str, hint: str) -> Any:
    """Read one setting, or explain what to do about its absence.

    ``hint`` is appended to the error, so the message says what the setting is
    for rather than only that it is missing.
    """
    value = get(name)
    if not value:
        raise ImproperlyConfigured(f"{name} is not set. {hint}")
    return value


def install_swappable_defaults() -> None:
    """Give the two swappable model settings a default, if the project has not.

    ``Meta.swappable`` is not like a normal setting lookup. Django's migration
    autodetector reads the named setting with a bare ``getattr(settings, name)``
    and lets the ``AttributeError`` escape, which is why ``AUTH_USER_MODEL`` --
    the pattern this follows -- is declared in ``django.conf.global_settings``.
    A third party app cannot add to that module, so the default has to be put
    into the project's settings instead.

    Timing is what makes this work and why it lives at import time in
    ``apps.py`` rather than in ``AppConfig.ready``. ``apps.populate`` runs in two
    phases: it creates every ``AppConfig`` first (importing each app module and
    its ``apps`` module), and only then imports any models. So this has already
    run by the time a field definition or the autodetector asks for either
    setting, and ``ready()`` would be far too late.

    A project that *has* set either one is left alone, which is the whole point:
    these are defaults, not overrides.
    """
    for name, default in (
        (AUDIT_SCOPE_MODEL, DEFAULT_SCOPE_MODEL),
        (AUDIT_IDENTITY_MODEL, DEFAULT_IDENTITY_MODEL),
    ):
        if not hasattr(settings, name):
            setattr(settings, name, default)
