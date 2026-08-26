"""Resolving the swappable models, the ``get_user_model`` way.

``settings.AUDIT_SCOPE_MODEL`` and ``settings.AUDIT_IDENTITY_MODEL`` hold
``"app_label.ModelName"`` strings. Field definitions can use those strings
directly, but runtime code needs the class -- and must not import *the model*
at module scope, because this app's modules are imported while the app registry
is still populating. Every function here resolves lazily, at call time.

``django.db.models`` itself is a different matter and is imported normally. It
must not go under ``TYPE_CHECKING``: this module has no
``from __future__ import annotations``, so on Python 3.14 the annotations below
are evaluated the moment anything reads ``__annotations__`` -- which Django and
most dependency-injection wiring do while introspecting a module -- and the name
would not be bound.
"""

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured

# Moving this import is what broke 0.1.0, and the rule will keep offering to.
from django.db import models  # noqa: TC002

from vinta_audit_logs import conf


def _resolve(setting_name: str, default: str) -> type[models.Model]:
    """Resolve one ``app_label.ModelName`` setting to the model class."""
    value = conf.get(setting_name, default)
    if not value:
        raise ImproperlyConfigured(
            f"{setting_name} must be an 'app_label.ModelName' string, not {value!r}."
        )
    try:
        return apps.get_model(value, require_ready=False)
    except ValueError as exc:
        raise ImproperlyConfigured(
            f"{setting_name} must be of the form 'app_label.ModelName', got {value!r}."
        ) from exc
    except LookupError as exc:
        raise ImproperlyConfigured(
            f"{setting_name} refers to model {value!r} that has not been installed."
        ) from exc


def get_audit_scope_model() -> type[models.Model]:
    """The scope model this installation uses."""
    return _resolve(conf.AUDIT_SCOPE_MODEL, conf.DEFAULT_SCOPE_MODEL)


def get_audit_identity_model() -> type[models.Model]:
    """The identity model this installation uses."""
    return _resolve(conf.AUDIT_IDENTITY_MODEL, conf.DEFAULT_IDENTITY_MODEL)
