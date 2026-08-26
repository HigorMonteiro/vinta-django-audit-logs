"""Settings for the test project: the models this app ships, unswapped."""

from __future__ import annotations

SECRET_KEY = "vinta-django-audit-logs-tests"
DEBUG = False
USE_TZ = True

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "vinta_audit_logs",
    "tests.testapp",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "tests.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

STATIC_URL = "/static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# The suite exercises the write path directly rather than through a broker, so
# records are persisted in-process. The Celery dispatcher gets its own test.
AUDIT_RECORD_DISPATCHER = "vinta_audit_logs.dispatch.dispatch_inline"
AUDIT_SERVICE_FACTORY = "tests.factories.build_audit_service"
AUDIT_REPOSITORY_FACTORY = "tests.factories.build_audit_repository"

LOGGING_CONFIG = None
