"""Portable DTOs: what an audit record is, independent of where it is stored.

Nothing here imports a model. That is the point -- these are the objects that
cross the task broker, travel between repositories, and get compared in tests,
so they hold values rather than rows and mean the same thing in a Django ORM
backend, an in-memory one, and a warehouse that has never heard of Django.

The three reference types are the vocabulary:

* :class:`ScopeRef` -- what a record belongs to.
* :class:`IdentitySnapshot` -- who acted, captured at emit time.
* :class:`SubjectRef` -- what was acted on.

Each has a matching *key* type holding only the identifying half, which is what
``AuditQuery`` filters on. Snapshot fields never participate in a filter: the
display name an actor happened to carry is not part of who they are.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, fields, replace
from typing import TYPE_CHECKING

from vinta_audit_logs.constants import IdentityType, ScopeType

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True)
class ScopeRef:
    """What a record belongs to, as portable values.

    ``scope_key`` is the string the log is indexed and partitioned by, so it has
    to be stable for the life of the scope: records already written under an old
    key do not follow a new one.

    ``label`` is carried for the benefit of a scope row that has to be created
    on first sight; it is not part of the scope's identity and never filters.
    """

    scope_type: str = ScopeType.GLOBAL
    scope_key: str = ""
    label: str = ""

    @property
    def key(self) -> ScopeKey:
        """This scope's identity, label dropped -- ready to filter with."""
        return ScopeKey(scope_type=self.scope_type, scope_key=self.scope_key)

    @classmethod
    def global_scope(cls) -> ScopeRef:
        """The scope for actions that belong to the installation, not a tenant."""
        return cls(scope_type=ScopeType.GLOBAL, scope_key="")


@dataclass(frozen=True)
class ScopeKey:
    """The identity half of a scope: what ``AuditQuery.scopes`` matches on."""

    scope_type: str
    scope_key: str


@dataclass(frozen=True)
class IdentitySnapshot:
    """Who acted, captured synchronously at emit time.

    Captured in the request rather than read back in the worker, because every
    field here is mutable state that may have changed -- or been deleted --
    by the time the write runs. An audit trail that re-reads the actor's groups
    at write time records the wrong groups.

    ``identity_type`` and ``identity_key`` together identify the actor; ids are
    only unique within a type, so a user 7 and an API token 7 are different
    actors and the pair travels together. ``identity_key`` is "" for an actor
    with no id at all -- the system acting on its own behalf.

    ``metadata`` is the project's extension point: a membership role, a token's
    scopes, whatever else the trail should remember about this actor. It is
    stored as-is and never filtered on.
    """

    identity_type: str = IdentityType.SYSTEM
    identity_key: str = ""
    identity_label: str = ""
    user_id: int | str | None = None
    is_staff: bool = False
    is_superuser: bool = False
    group_names: list[str] = field(default_factory=list)
    permission_keys: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def ref(self) -> IdentityRef:
        """This actor's identity, snapshot fields dropped -- ready to filter with."""
        return IdentityRef(identity_type=self.identity_type, identity_key=self.identity_key)


@dataclass(frozen=True)
class IdentityRef:
    """The identity half of an actor: what ``AuditQuery.actors`` matches on.

    Both fields are required. ``identity_key=""`` means the actor genuinely has
    no id, not "any id of this type"; to match every actor of a type regardless
    of id, use ``AuditQuery.actor_types``.
    """

    identity_type: str
    identity_key: str


@dataclass(frozen=True)
class SubjectRef:
    """Soft reference to the thing an action was taken on.

    Portable across any backend, survives deletion of the row it names, and
    carries no ORM coupling. ``subject_type`` is ``"app_label.modelname"``.
    """

    subject_type: str
    subject_id: str
    subject_label: str = ""

    @property
    def key(self) -> SubjectKey:
        """This subject's identity, label dropped -- ready to filter with."""
        return SubjectKey(subject_type=self.subject_type, subject_id=self.subject_id)


@dataclass(frozen=True)
class SubjectKey:
    """The identity half of a subject: what ``AuditQuery.subjects`` matches on."""

    subject_type: str
    subject_id: str


@dataclass(frozen=True)
class AuditRecordData:
    """A record as it is handed to a repository to be written.

    What ``AuditService.record`` builds and enqueues, and what a sync hands from
    one repository to another. Every field is either a scalar or one of the
    reference types above, so the whole thing survives a round trip through JSON.
    """

    action_key: str
    actor: IdentitySnapshot
    subject: SubjectRef
    scope: ScopeRef = field(default_factory=ScopeRef.global_scope)
    # The identity behind the actor, when one principal acted as another.
    on_behalf_of: IdentitySnapshot | None = None
    # Identities the action affected, as distinct from the one that took it.
    affected: list[IdentitySnapshot] = field(default_factory=list)
    # ``{field: {"old": ..., "new": ...}}``; None unless there was a change.
    diff: dict | None = None
    # Display label for the action, used only when the ``AuditAction`` row has
    # to be created on first sight of this key.
    action_name: str = ""
    # The model the action is about, when it is about one -- again only used
    # when the action row is created. "" otherwise.
    action_content_type_key: str = ""

    # Stable identity of this record ACROSS repositories. Generated once, at
    # emit time, and carried unchanged into every backend the record is written
    # to. It is what makes every write an upsert: re-persisting the same record
    # (a retried task, a re-run backfill) targets the existing row instead of
    # appending a duplicate. The primary key cannot serve this purpose -- it is
    # assigned per backend, so two copies of one record hold different ids.
    #
    # Version 7, so the value is time-ordered: the unique index every repository
    # keeps on this column takes its inserts at the index's right edge rather
    # than scattered across it.
    uid: uuid.UUID = field(default_factory=uuid.uuid7)

    # Emit time, captured synchronously. None means "let the repository stamp
    # its own clock", which is only correct for the FIRST write of a record --
    # a replica must reuse the value the record already carries, otherwise the
    # copies drift apart and no longer compare equal.
    created_at: datetime | None = None


@dataclass(frozen=True)
class AuditRecord:
    """A record as a repository returns it: everything in the input, plus id.

    ``id`` is the backend's own key and differs between copies of one record;
    ``uid`` is the same in all of them.
    """

    id: int
    created_at: datetime
    action_key: str
    actor: IdentitySnapshot
    subject: SubjectRef
    scope: ScopeRef = field(default_factory=ScopeRef.global_scope)
    on_behalf_of: IdentitySnapshot | None = None
    affected: list[IdentitySnapshot] = field(default_factory=list)
    diff: dict | None = None
    action_name: str = ""
    action_content_type_key: str = ""
    uid: uuid.UUID = field(default_factory=uuid.uuid7)

    def to_data(self) -> AuditRecordData:
        """Reduce this record back to the portable input DTO, identity intact.

        Used by the replication and sync paths: writing the result into another
        repository must preserve ``uid`` (so the write upserts rather than
        appends) and ``created_at`` (so the copy carries the original emit time
        rather than the replica's write time). ``id`` is deliberately dropped --
        it belongs to the repository this record was read from.
        """
        return AuditRecordData(
            action_key=self.action_key,
            actor=self.actor,
            subject=self.subject,
            scope=self.scope,
            on_behalf_of=self.on_behalf_of,
            affected=list(self.affected),
            diff=self.diff,
            action_name=self.action_name,
            action_content_type_key=self.action_content_type_key,
            uid=self.uid,
            created_at=self.created_at,
        )


@dataclass(frozen=True)
class AuditQuery:
    """Backend-agnostic filter object for repository reads.

    Every repository -- ORM-backed or not -- accepts this same object and must
    give it the same meaning. ``vinta_audit_logs.filtering`` holds the reference
    implementation of those semantics in pure Python; the ORM repository
    translates them to SQL. When a field is added here, both must learn it.

    **Every filter is a set membership test** (SQL's ``IN``), so one query
    covers "this one action" and "any of these six" without a second field per
    filter. Match one value by passing a one-element list.

    Combination rules, uniformly:

    * Fields AND together -- a record must satisfy every non-None field.
    * Values within a field OR together.
    * ``None`` means "this filter is not active".
    * An ``[]`` EMPTY list is an active filter that nothing satisfies, exactly
      like SQL's ``IN ()``. It is not the same as ``None``, and the difference
      matters: code that builds a filter from a computed set gets an empty
      result rather than silently querying everything.

    The exceptions to the list shape are the ones where a set makes no sense:
    the ``created_after`` / ``created_before`` range, and the tri-state
    ``has_diff`` / free-text ``search``.
    """

    # scope_key IN (...) -- the common read, and the cheap one: it hits the
    # denormalized column every browse index leads with, no join involved.
    scope_keys: list[str] | None = None
    # scope_type IN (...) -- every scope of these kinds. Use it with no
    # scope_keys to ask for "everything global" or "everything tenant-scoped".
    scope_types: list[str] | None = None
    # (scope_type, scope_key) IN (...) -- these specific scopes.
    scopes: list[ScopeKey] | None = None
    # Cross-repository identities. Used by the sync path to ask a target
    # repository which of a batch it already holds.
    uids: list[uuid.UUID] | None = None
    # action_key IN (...)
    actions: list[str] | None = None
    # identity_type IN (...) -- every actor of these kinds, whatever their id.
    actor_types: list[str] | None = None
    # (identity_type, identity_key) IN (...) -- these specific actors.
    actors: list[IdentityRef] | None = None
    # subject_type IN (...) -- every subject of these kinds, whatever their id.
    subject_types: list[str] | None = None
    # (subject_type, subject_id) IN (...) -- these specific subjects.
    subjects: list[SubjectKey] | None = None
    # Records affecting ANY of these identities.
    affected: list[IdentityRef] | None = None
    # Half-open range [created_after, created_before): the lower bound is
    # inclusive and the upper exclusive, so consecutive windows tile without
    # overlapping or dropping the record that lands exactly on a boundary.
    # That is what lets a sync walk a log in windows.
    created_after: datetime | None = None
    created_before: datetime | None = None
    # True: only records carrying a diff. False: only records without one.
    has_diff: bool | None = None
    # Free-text, case-insensitive, across the subject and actor label columns --
    # see vinta_audit_logs.filtering for the exact set.
    search: str | None = None

    def narrowed_to_uids(self, uids: list[uuid.UUID]) -> AuditQuery:
        """Return a copy of this query additionally restricted to ``uids``."""
        return replace(self, uids=list(uids))

    def active_extension_fields(self) -> tuple[str, ...]:
        """Names of any subclass-added filter that is currently active.

        A project whose swappable scope or identity model carries extra columns
        will want to filter on them, and the only honest way to do that is to
        subclass this and teach one repository about the new fields (see
        ``audit_integration.types`` for a worked example).

        The hazard that creates is silence: hand such a query to a *different*
        backend and the extra fields are simply not applied, so the caller gets
        more records than they asked for and no indication of it. Everything
        needed to catch that is here -- the base class knows its own fields, so
        anything else that is not None came from a subclass.

        Returns:
            The active extension field names, empty for a plain ``AuditQuery``.
        """
        base_fields = {f.name for f in fields(AuditQuery)}
        return tuple(
            f.name
            for f in fields(self)
            if f.name not in base_fields and getattr(self, f.name) is not None
        )


@dataclass(frozen=True)
class AuditPage:
    """Paginated audit records returned by query."""

    items: list[AuditRecord]
    total: int


@dataclass(frozen=True)
class AuditSyncResult:
    """Outcome of backfilling one repository from another.

    Returned by ``AuditService.sync_repository``. ``written`` counts records
    handed to the target's ``bulk_add`` -- because that write is an upsert, it
    counts records *reconciled*, not rows inserted, and re-running a completed
    sync reports the same number again with no duplicates created.
    """

    source: str
    target: str
    read: int = 0
    written: int = 0
    failed: int = 0
    # One entry per batch that raised, in the order the batches ran. A sync
    # keeps going after a failed batch so one bad chunk cannot strand the rest.
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when every record read was written."""
        return self.failed == 0
