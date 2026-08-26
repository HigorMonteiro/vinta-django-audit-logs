"""Resolving the swappable models, the ``get_user_model`` way.

``settings.AUDIT_SCOPE_MODEL`` and ``settings.AUDIT_IDENTITY_MODEL`` hold
``"app_label.ModelName"`` strings. Field definitions can use those strings
directly, but runtime code needs the class -- and must not import it at module
scope, because this app's modules are imported while the app registry is still
populating. Every function here resolves lazily, at call time.
"""

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db import models

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
