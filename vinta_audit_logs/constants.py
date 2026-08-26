"""Values the audit log itself defines.

Everything here is deliberately small. An audit log's vocabulary belongs to the
project keeping the log -- its actions, its kinds of actor, the shape of its
scopes -- so this module ships only what the app needs to function and leaves
the rest to the installing project.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _


class ScopeType(models.TextChoices):
    """Whether a record belongs to one scope or to the installation at large.

    ``GLOBAL`` is for actions with no tenant behind them -- a deployment, a
    platform-wide setting change. ``SCOPED`` is everything that happens inside
    one tenant, project, workspace, or whatever the installing project's
    boundary is called.
    """

    GLOBAL = ("global", _("Global"))
    SCOPED = ("scoped", _("Scoped"))


class IdentityType(models.TextChoices):
    """The kinds of actor this app ships with.

    Deliberately not passed as ``choices`` on the identity model: a project
    installing this app has actor kinds of its own -- a webhook sender, a
    single-use code, an inbound email -- and should be able to store them
    without a migration or a fork. These are the values the app itself uses.
    """

    USER = ("user", _("User"))
    SYSTEM = ("system", _("System"))
    SERVICE = ("service", _("Service"))


class AuditActionKey(models.TextChoices):
    """The three verbs every audit log needs on day one.

    Projects add their own keys as they instrument call sites --
    ``"calendar.event.reschedule"``, ``"billing.invoice.void"`` -- by creating
    ``AuditAction`` rows. Nothing here has to be extended for that to work; the
    key is a string column, and this enum is a convenience, not a registry.
    """

    CREATE = ("create", _("Create"))
    UPDATE = ("update", _("Update"))
    DELETE = ("delete", _("Delete"))
