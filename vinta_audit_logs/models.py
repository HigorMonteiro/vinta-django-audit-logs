"""Audit log models.

Four tables, and the split between them is the whole design:

``Audit`` is the log. Append-only, written once at emit time and never updated,
which is what lets it denormalize freely -- a copied value on an immutable row
has no opportunity to drift from the row it was copied from.

``AuditScope``, ``AuditIdentity`` and ``AuditAction`` are dimensions the log
points at. All three are owned by this app and none of them are ever deleted,
which is what makes it safe for ``Audit`` to hold real foreign keys to them:
the key refuses a bad id at write time, and ``PROTECT`` means no cascade can
ever reach the log. Rows the *host project* owns -- a user, a content type --
are held at arm's length instead, behind a nullable reference plus a snapshot
that stays readable after the row is gone.

Scope and identity are swappable (``AUDIT_SCOPE_MODEL`` / ``AUDIT_IDENTITY_MODEL``,
the ``AUTH_USER_MODEL`` pattern), because those are the two things whose *shape*
genuinely differs between projects: one has organizations, another workspaces,
another no tenant at all; one authenticates users, another also service
accounts and API tokens. Actions do not vary that way -- a project extends them
by adding rows -- so ``AuditAction`` is concrete.
"""

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import F
from django.utils import timezone

from vinta_audit_logs import conf
from vinta_audit_logs.constants import IdentityType, ScopeType

#: The models the foreign keys below point at, resolved at import time because a
#: field definition needs a target now. Both default to the models this app
#: ships, so an installation that has not overridden them still works -- the
#: swappable machinery reads the same settings and simply finds nothing to swap.
SCOPE_MODEL = conf.get(conf.AUDIT_SCOPE_MODEL, conf.DEFAULT_SCOPE_MODEL)
IDENTITY_MODEL = conf.get(conf.AUDIT_IDENTITY_MODEL, conf.DEFAULT_IDENTITY_MODEL)


class TimeStampedModel(models.Model):
    """Local ``created``/``modified`` pair.

    Deliberately not imported from the host project: this app is meant to be
    installed, so it cannot reach into a project's base model. Neither column
    is indexed -- these tables are either append-only (where ``Audit`` has its
    own ``created_at``) or small enough that a scan is free.
    """

    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AbstractAuditScope[ScopeValue](TimeStampedModel):
    """What a record belongs to: a tenant, a workspace, or the whole install.

    Subclasses decide what a scope *is* by implementing the ``scope`` property
    over whatever columns suit them -- a string, a foreign key, a composite --
    while this class owns the one rule that holds whatever they choose:
    ``scope_type`` and ``scope`` agree, always.

    ``scope_key`` is the portable spelling of that value: a string every backend
    can index, partition on, and carry to another repository unchanged. Concrete
    subclasses derive it from their own columns in :meth:`build_scope_key`.
    """

    scope_type = models.CharField(
        max_length=20,
        choices=ScopeType.choices,
        default=ScopeType.GLOBAL,
    )

    # The scope as a string, maintained by ``save``. This is what ``Audit``
    # copies onto every row and what its indexes lead with, so it has to be
    # stable, unique per scope, and meaningful without a join.
    scope_key = models.CharField(max_length=255, blank=True, db_index=True)

    # Human-readable name for admin and exports. A live value, not a snapshot:
    # a scope is a thing that still exists, unlike the actor and subject a
    # record describes.
    label = models.CharField(max_length=255, blank=True)

    class Meta:
        abstract = True

    def __str__(self) -> str:
        return self.label or self.scope_key or str(ScopeType(self.scope_type).label)

    def save(self, *args, **kwargs):
        self.validate_scope()
        self.scope_key = self.build_scope_key()
        if (
            update_fields := kwargs.get("update_fields")
        ) is not None and "scope_key" not in update_fields:
            # A partial update that moves the scope but leaves ``scope_key``
            # behind would silently detach the scope from its own audit
            # records, so add the column rather than let the write proceed.
            kwargs["update_fields"] = [*update_fields, "scope_key"]
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        self.validate_scope()

    @property
    def scope(self) -> ScopeValue | None:
        raise NotImplementedError("Needs to be implemented on subclass")

    @scope.setter
    def scope(self, value: ScopeValue | None):
        raise NotImplementedError("Needs to be implemented on subclass")

    @scope.deleter
    def scope(self):
        raise NotImplementedError("Needs to be implemented on subclass")

    def build_scope_key(self) -> str:
        """Return the portable string form of this scope.

        Must be stable for the life of the scope and unique among scopes of the
        same ``scope_type`` -- ``Audit`` rows are found by this value, so a key
        that changes orphans every record already written under the old one.

        Returns:
            The key, or "" for a global scope.
        """
        raise NotImplementedError("Needs to be implemented on subclass")

    def validate_scope(self):
        """Reject a row whose scope value and scope type disagree.

        Checked against the *final* state rather than against what changed, so
        insert and update run the identical rule and a partial update cannot
        slip a mismatch through by touching only one of the two fields.

        This is a convenience check, not the guarantee: ``save`` is bypassed by
        ``bulk_create`` and ``QuerySet.update``, so concrete subclasses are
        expected to carry a CHECK constraint saying the same thing.
        """
        scope_type_is_global = self.scope_type == ScopeType.GLOBAL
        if scope_type_is_global is not (self.scope is None):
            raise ValueError("The scope value and scope type fields do not match")


class AuditScope(AbstractAuditScope[str]):
    """The scope model this app ships: an opaque string.

    Enough for a project with no tenant concept, and the default the swappable
    setting points at. A project with a real boundary points
    ``AUDIT_SCOPE_MODEL`` at its own subclass instead and gets a foreign key,
    a label, and whatever else it needs on the scope row.
    """

    # Underscore-prefixed because ``scope`` itself is the property above; this
    # is the column behind it. Non-nullable, with "" as the absent value -- an
    # empty string participates in constraints and indexes where NULL would not.
    _scope = models.CharField(max_length=255, blank=True)

    class Meta:
        swappable = "AUDIT_SCOPE_MODEL"
        constraints: ClassVar = [
            # The invariant ``validate_scope`` checks, held where ``save``
            # cannot reach: ``bulk_create`` and ``QuerySet.update`` never call
            # it.
            models.CheckConstraint(
                condition=(
                    models.Q(scope_type=ScopeType.GLOBAL, _scope="")
                    | ~models.Q(scope_type=ScopeType.GLOBAL) & ~models.Q(_scope="")
                ),
                name="audit_scope_type_and_value_agree",
            ),
            models.UniqueConstraint(
                fields=["scope_type", "scope_key"],
                name="audit_scope_unique_key_per_type",
            ),
        ]

    @property
    def scope(self) -> str | None:
        return self._scope if self._scope != "" else None

    @scope.setter
    def scope(self, value: str | None):
        self._scope = value if value is not None else ""

    @scope.deleter
    def scope(self):
        self._scope = ""
        self.scope_type = ScopeType.GLOBAL

    def build_scope_key(self) -> str:
        return self._scope


class AbstractAuditIdentity(TimeStampedModel):
    """Who acted, captured as it was at the moment of the action.

    **One row per audit record**, not one per actor. The columns below are a
    snapshot: the groups, permissions and display name the actor carried when
    they acted, which is the question an audit trail is asked -- not what they
    carry now. Deduplicating rows per user would answer the wrong question.

    Not every actor is a person. A scheduled job, an API token and an internal
    service all take auditable actions, so ``user`` is optional and the columns
    that identify the actor -- ``identity_type``, ``identity_key``,
    ``identity_label`` -- are always populated whether or not a row in the user
    table backs them.

    Those columns are also what makes the record survive the actor. ``user`` is
    ``SET_NULL`` rather than ``PROTECT`` so deleting a user (an erasure request,
    an offboarding) neither fails nor takes the audit trail with it; what the
    user *was* stays legible afterwards.
    """

    # One of ``IdentityType``, or a value the installing project defines. No
    # ``choices``: see ``IdentityType``.
    identity_type = models.CharField(max_length=32, default=IdentityType.USER)

    # Stable identifier for the actor, as a string so it holds a user pk, a
    # token id, or a job name equally well. For a user identity this is the
    # user's pk at emit time, which is what keeps the row identifiable once
    # ``user`` has been nulled out. "" for an actor with no id at all -- the
    # system acting on its own behalf.
    identity_key = models.CharField(max_length=255, blank=True)

    # Human-readable name at emit time. A snapshot, not a live lookup: the
    # display name an actor had when they acted is the one the trail should
    # show.
    identity_label = models.CharField(max_length=255, blank=True)

    # Live link to the actor when the actor is a user and still exists. Nulled
    # on deletion; the snapshot columns above carry on without it.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    # --- authorization snapshot ---
    # Values as they stood when the action happened. Groups and permissions are
    # stored by name rather than by relation on purpose: an M2M would describe
    # the actor's groups *now*, would break when a Group or Permission row is
    # deleted, and would cost extra writes on a path that runs on every audited
    # action. JSON lists of strings answer "what could they do at the time".
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)
    group_names = models.JSONField(default=list, blank=True)
    permission_keys = models.JSONField(default=list, blank=True)

    # Whatever else the project captured about this actor at emit time -- a
    # membership role, an API token's scopes, the tenant a token was restricted
    # to. Here rather than in project columns so a project can extend the
    # snapshot without swapping the model out; a project that wants real
    # columns swaps it out and gets both.
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        abstract = True
        indexes: ClassVar = [
            models.Index(fields=["identity_type", "identity_key"]),
        ]

    def __str__(self) -> str:
        return self.identity_label or f"{self.identity_type}:{self.identity_key}"


class AuditIdentity(AbstractAuditIdentity):
    """The identity model this app ships. See :class:`AbstractAuditIdentity`."""

    class Meta(AbstractAuditIdentity.Meta):
        abstract = False
        swappable = "AUDIT_IDENTITY_MODEL"


class AuditAction(TimeStampedModel):
    """One row per distinct thing that can be done.

    A table rather than a column of free strings so that a mistyped action key
    fails at write time instead of producing a record that silently matches no
    filter. ``AuditService`` resolves keys through a process-local cache, so the
    lookup costs a dict hit after the first sighting of each key.

    Concrete, not swappable: an action is a key and a label in every project
    that would install this app, and projects extend it by adding rows.
    """

    # The model this action applies to, when it applies to one. Optional: many
    # actions ("user.impersonate", "export.run") are not about a single model.
    content_type = models.ForeignKey(
        "contenttypes.ContentType",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    # "app_label.modelname", and the durable half of that pair -- content type
    # rows are deleted routinely (``remove_stale_contenttypes`` runs after every
    # migrate that drops a model), and a content type id means nothing in
    # another database. "" when the action is not tied to a model.
    content_type_key = models.CharField(max_length=255, blank=True)

    # What callers pass and what the log is filtered by.
    key = models.CharField(max_length=255)
    # Display label. Free to change without disturbing anything.
    name = models.CharField(max_length=255)

    class Meta:
        constraints: ClassVar = [
            # ``key`` is what callers look up and what ``Audit.action`` resolves
            # against, so it is the half that has to be unique -- ``name`` is a
            # display label and must stay free to change.
            models.UniqueConstraint(
                fields=["content_type_key", "key"],
                name="audit_action_unique_key_per_content_type",
            ),
        ]

    def __str__(self) -> str:
        return self.name or f"{self.content_type_key}:{self.key}"


class Audit(models.Model):
    """One immutable record of one action taken by one actor on one subject.

    Every column is populated at emit time and never mutated afterwards. There
    is no ``modified``, no soft-delete flag and no update path: the repository
    writes and reads, and the only write that touches an existing row is an
    upsert on ``uid`` that rewrites it with identical content.
    """

    # Cross-repository identity. Generated once, at emit time, and carried
    # unchanged into every repository the record is written to, so that writing
    # the same record twice -- a retried task, a re-run backfill, a replica
    # catching up -- upserts the one row instead of appending a copy. ``id``
    # cannot do this job: it is assigned per backend, so two copies of one
    # record hold different ids. The unique index is the ``ON CONFLICT`` target
    # of every write, so it is load-bearing, not just a guard.
    #
    # UUID *version 7* (RFC 9562), not 4: the first 48 bits are a millisecond
    # timestamp, so values arrive in roughly generation order and the unique
    # index takes its inserts at its right edge instead of scattered across the
    # whole tree. On a table that only ever appends, and at audit-log volume,
    # that is the difference between a hot index that stays cached and one that
    # page-splits its way to bloat.
    uid = models.UUIDField(default=uuid.uuid7, unique=True, editable=False)

    # ``default=timezone.now`` rather than ``auto_now_add=True`` for two
    # reasons. It lets the service stamp the moment the audited action
    # *happened* rather than the moment a worker got around to the write, which
    # is the timestamp an audit trail is supposed to carry. And it lets a
    # replica reuse the value the record already carries -- ``auto_now_add``
    # overwrites any assigned value in ``pre_save``, which would give every copy
    # of a record a different timestamp and break both ordering agreement
    # between repositories and the windows a sync runs under.
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    # --- action ---
    # The action, twice over. The foreign key is what refuses a key that does
    # not exist -- a mistyped action string fails at write time instead of
    # producing a record that silently matches no filter. ``action_key`` is the
    # same value copied onto the row, so reading the log never joins and the
    # browse indexes below stand on their own.
    action = models.ForeignKey(AuditAction, on_delete=models.PROTECT, related_name="+")
    action_key = models.CharField(max_length=255)

    # --- scope ---
    # Same pairing, carrying more weight. Every browse of this log is scoped, so
    # leading the indexes with ``scope_key`` keeps them self-contained; and a
    # partition key has to be a value on the row, which rules out reaching a
    # surrogate ``scope_id`` through a join. Whatever this table is eventually
    # partitioned by -- ``created_at``, ``scope_key``, or both -- the column is
    # already here.
    scope = models.ForeignKey(SCOPE_MODEL, on_delete=models.PROTECT, related_name="+")
    scope_type = models.CharField(
        max_length=20,
        choices=ScopeType.choices,
        default=ScopeType.GLOBAL,
    )
    scope_key = models.CharField(max_length=255, blank=True)

    # --- actor ---
    # Third instance of the same pairing, for the same reason as scope and
    # action: "everything user 42 did in this tenant, newest first" is a first-
    # class question, and answering it through the identity table would mean a
    # join on every page of every audit browse. The copies keep the filter and
    # its index entirely on this row.
    actor = models.ForeignKey(IDENTITY_MODEL, on_delete=models.PROTECT, related_name="+")
    actor_type = models.CharField(max_length=32)
    actor_key = models.CharField(max_length=255, blank=True)
    # The identity behind the actor, when one principal acted as another: a
    # staff member impersonating a customer, a job running on someone's behalf.
    # The trail needs both -- who appeared to act, and who really did.
    on_behalf_of = models.ForeignKey(
        IDENTITY_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    # --- subject (soft reference: no cascade, survives the row it names) ---
    subject_content_type = models.ForeignKey(
        "contenttypes.ContentType",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    # A string, so it holds an integer pk, a UUID, or a natural key equally
    # well, and so the record does not change shape when the subject model does.
    subject_pk = models.CharField(max_length=255, db_index=True)
    # "app_label.modelname" snapshot, and the durable half of the pair: it stays
    # readable after ``django_content_type`` loses the row and it means the same
    # thing in every repository the record is copied to, which a per-database
    # content type id does not. "" = not captured.
    subject_content_type_key = models.CharField(max_length=255, blank=True)
    # Human-readable name of the subject at emit time. "" = not captured;
    # deliberately not auto-derived from ``str(instance)``, which can
    # dereference related rows and raise inside the action being audited.
    subject_label = models.CharField(max_length=255, blank=True)

    # --- payload ---
    # ``{field: {"old": ..., "new": ...}}``. Always either None or a NON-EMPTY
    # dict -- the repository normalizes ``{}`` to None so the ``has_diff``
    # filter stays meaningful.
    diff = models.JSONField(null=True, blank=True)

    affected_identities = models.ManyToManyField(
        IDENTITY_MODEL,
        through="vinta_audit_logs.AuditAffectedIdentity",
        through_fields=("audit", "identity"),
        related_name="+",
        blank=True,
    )

    class Meta:
        # Each index leads with ``scope_key`` and ends with ``created_at``
        # descending: every read of this log is "the most recent records in one
        # scope, narrowed by something", so the index supplies the ordering and
        # the query does not sort. They key on the denormalized copies rather
        # than the foreign keys so no join is needed to use them -- and, for the
        # subject index, so it keeps working after a content type row is deleted
        # and ``subject_content_type`` goes NULL.
        indexes: ClassVar = [
            models.Index(F("scope_key"), F("created_at").desc(), name="audit_scope_recent_idx"),
            models.Index(
                F("scope_key"),
                F("action_key"),
                F("created_at").desc(),
                name="audit_scope_action_recent_idx",
            ),
            models.Index(
                F("scope_key"),
                F("actor_type"),
                F("actor_key"),
                F("created_at").desc(),
                name="audit_scope_actor_recent_idx",
            ),
            models.Index(
                F("scope_key"),
                F("subject_content_type_key"),
                F("subject_pk"),
                F("created_at").desc(),
                name="audit_scope_subject_recent_idx",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"Audit({self.action_key}, {self.actor_id}, "
            f"{self.subject_content_type_key}:{self.subject_pk})"
        )


class AuditAffectedIdentity(models.Model):
    """Links an ``Audit`` to the identities the action affected.

    Explicit rather than an auto-created M2M table for two reasons: it can carry
    its own index, and a partitioned ``Audit`` needs its referencing table
    written by hand anyway (a foreign key into a partitioned table has to
    include the partition key).

    ``CASCADE`` on ``audit`` is correct here and nowhere else in this app: a
    link row has no meaning without the record it belongs to, and audit records
    are not deleted.
    """

    audit = models.ForeignKey(Audit, on_delete=models.CASCADE, related_name="affected_links")
    identity = models.ForeignKey(IDENTITY_MODEL, on_delete=models.PROTECT, related_name="+")
    # Denormalized onto the link for the same reason the actor is denormalized
    # onto the record: "every audit record that touched this person" is answered
    # from this table alone, without joining to the identity rows.
    identity_type = models.CharField(max_length=32)
    identity_key = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["audit", "identity"],
                name="audit_affected_identity_unique",
            ),
        ]
        indexes: ClassVar = [
            models.Index(
                fields=["identity_type", "identity_key", "audit"],
                name="audit_affected_identity_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"AuditAffectedIdentity(audit={self.audit_id}, identity={self.identity_id})"
