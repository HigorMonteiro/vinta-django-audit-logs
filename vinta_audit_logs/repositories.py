"""Where audit records are stored, and how a portable record becomes a row.

``AuditRepository`` is the interface: read and append, with append defined as an
upsert on ``AuditRecordData.uid`` so that writing a record twice converges on the
one it already holds.

``DjangoORMAuditRepository`` implements it against this app's models, and is the
one place where a portable DTO has to meet a concrete schema. That meeting is
the extension point: a project whose scope or identity model carries extra
columns overrides :meth:`~DjangoORMAuditRepository.build_scope_defaults` and
:meth:`~DjangoORMAuditRepository.build_identity_defaults` to fill them, and
inherits everything else -- the upsert, the batching, the filter translation,
the streaming read.

``InMemoryAuditRepository`` is the second implementation, kept honest by running
its reads through ``vinta_audit_logs.filtering``.
"""

from __future__ import annotations

import abc
import operator
from functools import reduce
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.db.models import BooleanField, Expression, Prefetch, Q
from django.utils import timezone

from vinta_audit_logs.filtering import (
    DEFAULT_ORDERING,
    STABLE_ITERATION_ORDERING,
    apply_query,
    normalize_ordering,
)
from vinta_audit_logs.types import (
    AuditPage,
    AuditQuery,
    AuditRecord,
    AuditRecordData,
    IdentitySnapshot,
    ScopeRef,
    SubjectRef,
)

if TYPE_CHECKING:
    import uuid
    from collections.abc import Iterable, Iterator, Sequence

    from django.db import models


#: How many records ``iter_records`` pulls per round-trip when the caller does
#: not say. Big enough that walking a large log is not dominated by round-trips,
#: small enough that a page of records with diffs stays comfortably in memory.
DEFAULT_ITERATION_CHUNK_SIZE = 500

#: How many records the ORM repository upserts per ``bulk_create`` statement.
#: Bounds the size of a single ``INSERT ... ON CONFLICT`` and, with it, how long
#: one statement holds its locks.
DEFAULT_BATCH_SIZE = 500


class _RowComparison(Expression):
    """SQL row comparison -- ``(col_a, col_b) > (%s, %s)``.

    The ORM has no spelling for comparing a tuple of columns against a tuple of
    values, and Postgres compares row constructors lexicographically and will
    drive an index scan straight from one. That is what makes the keyset walk in
    ``iter_records`` cost the same on its ten-thousandth page as on its first.
    """

    def __init__(self, field_names: tuple[str, ...], operator: str, values: tuple[Any, ...]):
        super().__init__(output_field=BooleanField())
        self.field_names = field_names
        self.operator = operator
        self.values = values

    def as_sql(self, compiler, connection):
        meta = compiler.query.get_meta()
        table = connection.ops.quote_name(compiler.query.get_initial_alias())
        fields = [meta.get_field(name) for name in self.field_names]
        columns = ", ".join(
            f"{table}.{connection.ops.quote_name(field.column)}" for field in fields
        )
        # Each value goes through its own field's adapter rather than straight
        # into the parameter list. Building the SQL by hand skips the conversion
        # the ORM would normally do, and the backends disagree about what they
        # will accept raw -- psycopg adapts a UUID and a datetime happily, SQLite
        # rejects both.
        params = [
            field.get_db_prep_value(value, connection, prepared=False)
            for field, value in zip(fields, self.values, strict=True)
        ]
        placeholders = ", ".join(["%s"] * len(params))
        return f"({columns}) {self.operator} ({placeholders})", params


def _after_cursor(cursor: tuple[Any, Any]) -> Q:
    """Match only the records that sort strictly after ``cursor``.

    ``cursor`` is the ``(created_at, uid)`` of the last record already yielded.
    """
    return Q(_RowComparison(STABLE_ITERATION_ORDERING, ">", cursor))


def _dedupe_snapshots(snapshots: Iterable[IdentitySnapshot]) -> list[IdentitySnapshot]:
    """Drop repeated identities, keeping the first of each and the given order.

    Keyed on ``(identity_type, identity_key)`` rather than on the snapshot
    itself: a snapshot carries lists and a dict, so it is not hashable, and two
    snapshots of the same actor taken microseconds apart are the same actor
    regardless of any difference in what they captured.
    """
    seen: dict[tuple[str, str], IdentitySnapshot] = {}
    for snapshot in snapshots:
        seen.setdefault((snapshot.identity_type, snapshot.identity_key), snapshot)
    return list(seen.values())


def _or_group(conditions: Iterable[Q]) -> Q:
    """OR a series of Q objects into one, matching nothing when there are none.

    ``Q(pk__in=[])`` is the empty-set identity: an empty ``AuditQuery`` list
    field is an active filter that nothing satisfies, so folding zero conditions
    must produce "match nothing" rather than the bare ``Q()`` that would match
    everything.
    """
    conditions = list(conditions)
    if not conditions:
        return Q(pk__in=[])
    return reduce(operator.or_, conditions)


class AuditRepository(abc.ABC):
    """Backend-agnostic interface for audit record storage.

    Read + append only. No update, no delete -- with one deliberate nuance: an
    append is an **upsert keyed on** ``AuditRecordData.uid``, not a blind insert.
    A record carries the same ``uid`` into every repository it is written to, so
    writing one twice -- a retried task, a re-run backfill, a replica catching up
    on records it already received -- converges on the single existing record
    instead of appending a copy. That is what makes replication and sync safe to
    repeat, and it is a requirement of this interface, not an optimization an
    implementation may skip.

    ``query`` / ``count`` / ``iter_records`` all take the same ``AuditQuery``, so
    a caller can point the identical filter at any repository. The semantics of
    that filter are defined once in ``vinta_audit_logs.filtering``; an
    implementation that pushes the filters down to its store must agree with
    them, and one that cannot push them down can implement ``query`` by handing
    its records to ``apply_query``.
    """

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def add(self, data: AuditRecordData) -> AuditRecord:
        """Persist an audit record, upserting on ``data.uid``.

        Args:
            data: The record data to persist.

        Returns:
            The persisted AuditRecord with id and created_at populated.
        """
        ...

    @abc.abstractmethod
    def bulk_add(self, data: Sequence[AuditRecordData]) -> list[AuditRecord]:
        """Persist many audit records in as few round-trips as the backend allows.

        Same upsert-on-``uid`` contract as :meth:`add`, applied to a batch: the
        entry point for replication and for backfilling a repository from
        another one.

        Duplicate ``uid`` values *within* one call are the caller's problem to
        avoid; an implementation may collapse them or may raise.

        Args:
            data: The records to persist. May be empty.

        Returns:
            The persisted AuditRecords, in the order they were given.
        """
        ...

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def get(self, audit_id: int) -> AuditRecord | None:
        """Retrieve a single audit record by this backend's own id.

        Backend-local: the id of a record differs between repositories holding
        the same log. Use :meth:`get_by_uid` to look the same record up in more
        than one repository.

        Args:
            audit_id: The audit record id.

        Returns:
            The AuditRecord if found, None otherwise.
        """
        ...

    @abc.abstractmethod
    def query(
        self,
        q: AuditQuery,
        *,
        offset: int = 0,
        limit: int = 50,
        ordering: str | Sequence[str] = DEFAULT_ORDERING,
    ) -> AuditPage:
        """Query audit records with filters, pagination, and ordering.

        Args:
            q: The query filter/search object.
            offset: Number of records to skip (default 0).
            limit: Maximum records to return (default 50).
            ordering: Field or fields to order by, each with an optional ``-``
                prefix for descending. Values outside
                ``vinta_audit_logs.filtering.ALLOWED_ORDERING_FIELDS`` are
                dropped rather than raising; if none survive the default
                ordering applies.

        Returns:
            AuditPage containing items and total count.
        """
        ...

    def get_by_uid(self, uid: uuid.UUID) -> AuditRecord | None:
        """Retrieve a single audit record by its cross-repository identity.

        The portable lookup: unlike :meth:`get`, the same argument finds the
        same record in every repository the record was written to. Implemented
        here in terms of :meth:`query` so every backend has it; override when
        the backend can index ``uid`` directly.

        Args:
            uid: The record's stable identity.

        Returns:
            The AuditRecord if found, None otherwise.
        """
        page = self.query(AuditQuery(uids=[uid]), offset=0, limit=1)
        return page.items[0] if page.items else None

    def count(self, q: AuditQuery) -> int:
        """Count records matching ``q`` without fetching them.

        Implemented here via a zero-length page, which every ``query`` already
        has to total correctly. Override when the backend can count without
        building a page.

        Args:
            q: The filters to count under.

        Returns:
            The number of matching records.
        """
        return self.query(q, offset=0, limit=0).total

    def iter_records(
        self,
        q: AuditQuery,
        *,
        chunk_size: int = DEFAULT_ITERATION_CHUNK_SIZE,
    ) -> Iterator[AuditRecord]:
        """Stream every record matching ``q``, oldest first, in bounded memory.

        The read side of replication and sync: a caller walks a whole log
        without ever holding more than ``chunk_size`` records at once.

        Ordering is fixed to ``STABLE_ITERATION_ORDERING`` (``created_at``
        ascending, ``uid`` breaking ties) and is not a parameter, because the
        correctness of the walk depends on it. An audit log only appends, so
        ascending order puts rows written *during* the walk past the cursor
        rather than shifting rows already passed; the ``uid`` tiebreak stops
        same-instant records from reshuffling across a page boundary, which is
        how offset pagination loses a record.

        Args:
            q: The filters to walk under.
            chunk_size: Records to fetch per round-trip.

        Yields:
            Matching AuditRecords, oldest first.
        """
        offset = 0
        while True:
            page = self.query(
                q, offset=offset, limit=chunk_size, ordering=STABLE_ITERATION_ORDERING
            )
            if not page.items:
                return
            yield from page.items
            if len(page.items) < chunk_size:
                return
            offset += len(page.items)


class DjangoORMAuditRepository(AuditRepository):
    """ORM-backed implementation, and the seam where DTOs become rows.

    Writes are upserts on the unique ``uid`` column, via
    ``bulk_create(update_conflicts=True)`` -> ``INSERT ... ON CONFLICT (uid) DO
    UPDATE``. Persisting the same record twice therefore rewrites the one row
    rather than appending a second, which is what lets the task be retried and a
    backfill be re-run.

    Subclassing
    -----------
    A project that swaps ``AUDIT_SCOPE_MODEL`` or ``AUDIT_IDENTITY_MODEL`` for a
    model with extra columns overrides the two ``build_*_defaults`` hooks to
    populate them from the portable DTO, and the two ``*_to_*`` hooks to read
    them back. Nothing else needs touching: the upsert, the batching, the filter
    translation and the streaming read all work off the columns this app
    defines, which every swapped model still has.
    """

    #: Payload columns rewritten when an upsert hits an existing ``uid``.
    #: Everything a record carries except ``uid`` itself (the conflict target)
    #: and ``id`` (the row's own key).
    _UPSERT_UPDATE_FIELDS = (
        "created_at",
        "action",
        "action_key",
        "scope",
        "scope_type",
        "scope_key",
        "actor",
        "actor_type",
        "actor_key",
        "on_behalf_of",
        "subject_content_type",
        "subject_content_type_key",
        "subject_pk",
        "subject_label",
        "diff",
    )

    def __init__(self) -> None:
        # "app_label.modelname" -> content type id, or None for a model this
        # installation does not have.
        #
        # Safe to hold for the life of the repository -- which is usually the
        # life of the process -- because content type rows are written by
        # ``migrate`` and are already durable before any audit record exists.
        # Action ids get no such cache: see ``resolve_action_id``.
        self._content_type_id_cache: dict[str, int | None] = {}

    # ------------------------------------------------------------------
    # Hooks: portable DTO <-> concrete row
    # ------------------------------------------------------------------

    def build_scope_defaults(self, ref: ScopeRef) -> dict[str, Any]:
        """Column values for a scope row being created on first sight.

        Called only when no scope row matches ``(scope_type, scope_key)`` yet;
        an existing scope is reused as-is. The returned dict must fill every
        non-nullable column the installation's scope model adds beyond this
        app's own.

        Args:
            ref: The portable scope reference carried on the record.

        Returns:
            Field values for ``Model.objects.create``, excluding the lookup
            columns ``scope_type`` and ``scope_key``.
        """
        return {"_scope": ref.scope_key, "label": ref.label}

    def build_identity_defaults(self, snapshot: IdentitySnapshot) -> dict[str, Any]:
        """Column values for the identity row this record's actor gets.

        One row per record, per :class:`~vinta_audit_logs.models.AbstractAuditIdentity`
        -- the snapshot describes the actor at one moment, so it is not shared
        between records.

        A project whose identity model has real columns for what this app keeps
        in ``metadata`` (a membership role, a token's scopes) maps them here.

        Args:
            snapshot: The portable actor snapshot captured at emit time.

        Returns:
            Field values for ``Model.objects.create``.
        """
        return {
            "identity_type": snapshot.identity_type,
            "identity_key": snapshot.identity_key,
            "identity_label": snapshot.identity_label,
            "user_id": snapshot.user_id,
            "is_staff": snapshot.is_staff,
            "is_superuser": snapshot.is_superuser,
            "group_names": list(snapshot.group_names),
            "permission_keys": list(snapshot.permission_keys),
            "metadata": dict(snapshot.metadata),
        }

    def identity_to_snapshot(self, identity: models.Model) -> IdentitySnapshot:
        """Rebuild the portable snapshot from a stored identity row.

        The inverse of :meth:`build_identity_defaults`; a project that maps
        extra columns there reads them back here.
        """
        return IdentitySnapshot(
            identity_type=identity.identity_type,
            identity_key=identity.identity_key,
            identity_label=identity.identity_label,
            user_id=identity.user_id,
            is_staff=identity.is_staff,
            is_superuser=identity.is_superuser,
            group_names=list(identity.group_names or []),
            permission_keys=list(identity.permission_keys or []),
            metadata=dict(identity.metadata or {}),
        )

    def attach_identity_relations(
        self,
        identities: Sequence[models.Model],
        snapshots: Sequence[IdentitySnapshot],
    ) -> None:
        """Attach many-to-many rows to identities that were just bulk-created.

        A no-op here, because the shipped identity model has no relations to
        attach -- it keeps its authorization snapshot as JSON precisely so it
        does not need any. The hook exists for a project whose swapped-in model
        does, because ``bulk_create`` cannot populate a many-to-many and there is
        no other point at which the rows and their snapshots are both in hand.

        Called once per batch, with the two sequences in matching order.
        Implementations should write their link rows with ``bulk_create`` for the
        whole batch rather than assigning per identity: an audit write is already
        the cheapest part of the action it describes, and should stay that way.

        Args:
            identities: The rows just created, in order.
            snapshots: The snapshots they were built from, same order.
        """

    def scope_to_ref(self, scope: models.Model) -> ScopeRef:
        """Rebuild the portable reference from a stored scope row."""
        return ScopeRef(
            scope_type=scope.scope_type,
            scope_key=scope.scope_key,
            label=scope.label,
        )

    # ------------------------------------------------------------------
    # Resolution: refs to rows
    # ------------------------------------------------------------------

    def resolve_scope(self, ref: ScopeRef) -> models.Model:
        """Find or create the scope row a record belongs to.

        Deduplicated on ``(scope_type, scope_key)``, which the shipped model
        constrains as unique -- scopes are a small dimension, one row per tenant,
        not one per record.
        """
        from vinta_audit_logs.models_registry import get_audit_scope_model

        scope_model = get_audit_scope_model()
        scope, _created = scope_model.objects.get_or_create(
            scope_type=ref.scope_type,
            scope_key=ref.scope_key,
            defaults=self.build_scope_defaults(ref),
        )
        return scope

    def resolve_identity(self, snapshot: IdentitySnapshot) -> models.Model:
        """Create the identity row for one actor on one record.

        Always a create, never a lookup: the row is a point-in-time snapshot, so
        sharing it between records would make a later action rewrite the groups
        an earlier one recorded.
        """
        return self.resolve_identities([snapshot])[0]

    def resolve_identities(self, snapshots: Sequence[IdentitySnapshot]) -> list[models.Model]:
        """Create the identity rows for a whole batch in one statement.

        The batch form is the one that matters. A single audit record can carry
        an actor, the identity it acted on behalf of, and any number of affected
        parties, and a batch multiplies all of that by the number of records --
        so creating them one at a time is the difference between one INSERT and
        a thousand.

        No deduplication, deliberately, and none is possible: each row is a
        snapshot belonging to one record, so two records naming the same person
        still get a row each. That is what lets the log say what was true of
        that person at each moment rather than only at the latest one.

        Returns:
            The created rows, in the order the snapshots were given.
        """
        from vinta_audit_logs.models_registry import get_audit_identity_model

        if not snapshots:
            return []
        identity_model = get_audit_identity_model()
        identities = identity_model.objects.bulk_create(
            [identity_model(**self.build_identity_defaults(snapshot)) for snapshot in snapshots],
            batch_size=DEFAULT_BATCH_SIZE,
        )
        # Separate pass because bulk_create cannot populate a many-to-many, and
        # this is the only point where the rows and the snapshots they came from
        # are both in hand.
        self.attach_identity_relations(identities, snapshots)
        return identities

    def resolve_action_id(
        self, data: AuditRecordData, memo: dict[tuple[str, str], int] | None = None
    ) -> int:
        """Find or create the action row for a record's key.

        The foreign key is what makes a mistyped action fail at write time
        instead of producing a record that silently matches no filter.

        Deliberately **not** cached for the life of the repository, which is
        usually a long-lived singleton. An action id cached from a transaction
        that then rolled back is a foreign key to a row that never existed, and
        the next write fails on it -- a hazard that outlives the request that
        created it and is maddening to trace. Deferring the cache write to
        ``on_commit`` does not fix it either: a test harness that captures and
        runs those callbacks still populates the cache from a transaction it is
        about to roll back.

        The ``memo`` covers the case that actually costs anything: a batch of
        records sharing a handful of actions resolves each one once, rather than
        once per record. Beyond that a batch pays one indexed lookup per distinct
        action, which next to its own inserts is nothing.

        Args:
            data: The record whose action to resolve.
            memo: Per-call cache, shared across one ``bulk_add``.

        Returns:
            The action row's primary key.
        """
        from vinta_audit_logs.models import AuditAction

        cache_key = (data.action_content_type_key, data.action_key)
        if memo is not None and (action_id := memo.get(cache_key)) is not None:
            return action_id
        action, _created = AuditAction.objects.get_or_create(
            content_type_key=data.action_content_type_key,
            key=data.action_key,
            defaults={"name": data.action_name or data.action_key},
        )
        if memo is not None:
            memo[cache_key] = action.pk
        return action.pk

    def resolve_content_type_id(self, subject_type: str) -> int | None:
        """Map ``"app_label.modelname"`` to a content type id, or None.

        None is a normal outcome, not an error: the record may name a model this
        installation does not have (a record replicated from elsewhere, or one
        whose model has since been removed). ``subject_content_type_key`` carries
        the meaning either way -- the foreign key is a convenience for admin
        links, not the record's identity.
        """
        from django.contrib.contenttypes.models import ContentType

        if not subject_type:
            return None
        if subject_type in self._content_type_id_cache:
            return self._content_type_id_cache[subject_type]
        content_type_id: int | None = None
        if "." in subject_type:
            app_label, _, model_name = subject_type.partition(".")
            content_type = ContentType.objects.filter(
                app_label=app_label, model=model_name.lower()
            ).first()
            content_type_id = content_type.pk if content_type is not None else None
        self._content_type_id_cache[subject_type] = content_type_id
        return content_type_id

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def add(self, data: AuditRecordData) -> AuditRecord:
        """Persist an audit record and its affected-identity links.

        Upserts on ``data.uid``: see :meth:`bulk_add`, which this delegates to so
        the single-record and batch paths cannot drift apart.
        """
        return self.bulk_add([data])[0]

    def bulk_add(self, data: Sequence[AuditRecordData]) -> list[AuditRecord]:
        """Upsert a batch of audit records, their identities, and their links.

        Runs inside one ``transaction.atomic()`` so the dimension rows, the
        ``Audit`` rows and the through rows are committed together or not at all.

        The ``Audit`` rows go in with ``update_conflicts=True`` on ``uid``, so a
        record already present is rewritten in place. Postgres returns the ids of
        both inserted and updated rows, which is what lets the through rows be
        attached without a second lookup.

        Diff invariant: ``diff`` is always either None or a NON-EMPTY dict. An
        empty dict means "no changes" and is normalized to None here so that the
        ``has_diff`` filter stays meaningful.

        Args:
            data: The records to upsert, in any order. May be empty.

        Returns:
            The persisted AuditRecords, in the order given.
        """
        # Deferred import: this module is imported at app-load time, before the
        # model registry is ready.
        from vinta_audit_logs.models import Audit, AuditAffectedIdentity

        if not data:
            return []

        # One clock reading for the whole batch, so records emitted together and
        # left without a created_at do not fan out over the write's duration.
        now = timezone.now()

        with transaction.atomic():
            # Dimensions first: every Audit row needs its foreign keys to exist.
            scope_ids = {
                ref: self.resolve_scope(ref).pk
                for ref in dict.fromkeys(item.scope for item in data)
            }
            action_memo: dict[tuple[str, str], int] = {}
            action_ids = {
                item.uid: self.resolve_action_id(item, memo=action_memo) for item in data
            }
            # Every identity row this batch needs, created in one statement and
            # then handed back out. Building the flat list first is what keeps
            # this to a single INSERT no matter how many records, actors and
            # affected parties are involved.
            affected_per_record = {item.uid: _dedupe_snapshots(item.affected) for item in data}
            flat_snapshots: list[IdentitySnapshot] = []
            for item in data:
                flat_snapshots.append(item.actor)
                if item.on_behalf_of is not None:
                    flat_snapshots.append(item.on_behalf_of)
                flat_snapshots.extend(affected_per_record[item.uid])
            identity_rows = iter(self.resolve_identities(flat_snapshots))

            actor_ids: dict[uuid.UUID, int] = {}
            on_behalf_ids: dict[uuid.UUID, int | None] = {}
            affected_ids: dict[uuid.UUID, list[int]] = {}
            for item in data:
                actor_ids[item.uid] = next(identity_rows).pk
                on_behalf_ids[item.uid] = (
                    next(identity_rows).pk if item.on_behalf_of is not None else None
                )
                affected_ids[item.uid] = [
                    next(identity_rows).pk for _ in affected_per_record[item.uid]
                ]

            audits = [
                Audit(
                    uid=item.uid,
                    created_at=item.created_at or now,
                    action_id=action_ids[item.uid],
                    action_key=item.action_key,
                    scope_id=scope_ids[item.scope],
                    scope_type=item.scope.scope_type,
                    scope_key=item.scope.scope_key,
                    actor_id=actor_ids[item.uid],
                    actor_type=item.actor.identity_type,
                    actor_key=item.actor.identity_key,
                    on_behalf_of_id=on_behalf_ids[item.uid],
                    subject_content_type_id=self.resolve_content_type_id(
                        item.subject.subject_type
                    ),
                    subject_content_type_key=item.subject.subject_type,
                    subject_pk=item.subject.subject_id,
                    subject_label=item.subject.subject_label or "",
                    diff=(item.diff or None),
                )
                for item in data
            ]

            Audit.objects.bulk_create(
                audits,
                batch_size=DEFAULT_BATCH_SIZE,
                update_conflicts=True,
                unique_fields=["uid"],
                update_fields=list(self._UPSERT_UPDATE_FIELDS),
            )

            links = [
                AuditAffectedIdentity(
                    audit=audit,
                    identity_id=identity_id,
                    identity_type=snapshot.identity_type,
                    identity_key=snapshot.identity_key,
                )
                for item, audit in zip(data, audits, strict=True)
                for identity_id, snapshot in zip(
                    affected_ids[item.uid], affected_per_record[item.uid], strict=True
                )
            ]
            if links:
                # Ignore rather than update: the pair *is* the whole row, so
                # there is nothing left to rewrite, and re-persisting a record
                # leaves its existing links untouched.
                AuditAffectedIdentity.objects.bulk_create(
                    links, batch_size=DEFAULT_BATCH_SIZE, ignore_conflicts=True
                )

        # Reload to build the canonical DTOs. Reading back by uid (rather than
        # mapping the in-memory instances) is what makes the returned records
        # reflect what is actually stored after the upsert -- including the links
        # an already-present record brought with it.
        stored = {
            audit.uid: audit
            for audit in self._record_queryset().filter(uid__in=[item.uid for item in data])
        }
        missing = [item.uid for item in data if item.uid not in stored]
        if missing:
            raise RuntimeError(f"Audit rows {missing} disappeared immediately after being written")
        return [self._to_record(stored[item.uid]) for item in data]

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def get(self, audit_id: int) -> AuditRecord | None:
        """Retrieve a single audit record by id, or None if not found."""
        audit = self._record_queryset().filter(pk=audit_id).first()
        return self._to_record(audit) if audit is not None else None

    def get_by_uid(self, uid: uuid.UUID) -> AuditRecord | None:
        """Retrieve by cross-repository identity, straight off the unique index."""
        audit = self._record_queryset().filter(uid=uid).first()
        return self._to_record(audit) if audit is not None else None

    def count(self, q: AuditQuery) -> int:
        """Count matching records with a COUNT query and no row fetch."""
        return self._filtered_queryset(q).count()

    def query(
        self,
        q: AuditQuery,
        *,
        offset: int = 0,
        limit: int = 50,
        ordering: str | Sequence[str] = DEFAULT_ORDERING,
    ) -> AuditPage:
        """Filter, order and paginate.

        ``total`` is counted on the fully-filtered queryset before pagination, so
        callers always get the complete match count rather than the page size.
        """
        qs = self._filtered_queryset(q)
        total = qs.count()
        qs = qs.order_by(*normalize_ordering(ordering))
        page_qs = self._with_related(qs[offset : offset + limit])
        return AuditPage(items=[self._to_record(audit) for audit in page_qs], total=total)

    def iter_records(
        self,
        q: AuditQuery,
        *,
        chunk_size: int = DEFAULT_ITERATION_CHUNK_SIZE,
    ) -> Iterator[AuditRecord]:
        """Stream matching records, oldest first, seeking by key rather than offset.

        Same contract and same ordering as the interface's default, implemented
        so that walking a large log stays linear in its size.

        The default pages with ``OFFSET``, and ``OFFSET n`` makes the database
        produce and discard n rows before returning anything. Walking a million
        records in chunks of 500 that way costs two thousand queries whose price
        grows with every page -- the first chunk gets read two thousand times
        over -- which is quadratic in the size of the log. This instead seeks on
        ``(created_at, uid)``: the same pair the walk is ordered by, and a total
        order because ``uid`` is unique. Every chunk becomes an index range scan
        starting exactly where the previous one stopped, at constant cost however
        deep the walk goes.

        It also drops the ``COUNT(*)`` that ``query`` runs on every call, which a
        paginated read wants and a backfill does not.

        The seek predicate is the strict lexicographic
        ``(created_at, uid) > (last_created_at, last_uid)``, written as a row
        comparison. The obvious alternatives are both worse: ``created_at >= x
        AND uid > y`` is wrong (it drops records sharing that instant with a
        smaller uid), and an OR of two range conditions is correct but costs the
        planner its single index range.

        Args:
            q: The filters to walk under.
            chunk_size: Records to fetch per round-trip.

        Yields:
            Matching AuditRecords, oldest first.
        """
        base = self._filtered_queryset(q).order_by(*STABLE_ITERATION_ORDERING)
        cursor: tuple[Any, Any] | None = None
        while True:
            qs = base if cursor is None else base.filter(_after_cursor(cursor))
            page = list(self._with_related(qs[:chunk_size]))
            if not page:
                return
            yield from (self._to_record(audit) for audit in page)
            if len(page) < chunk_size:
                return
            cursor = (page[-1].created_at, page[-1].uid)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _record_queryset(self):
        """Base queryset with everything ``_to_record`` reads already loaded."""
        from vinta_audit_logs.models import Audit

        return self._with_related(Audit.objects.all())

    def _with_related(self, qs):
        """Attach the joins and prefetches ``_to_record`` needs.

        Applied to every read path, so mapping a page of records is a fixed
        number of queries rather than a number that grows with the page.

        The scope, actor and on-behalf-of rows join, because each record has at
        most one of each. The affected links cannot join without multiplying the
        page, so they prefetch -- and the prefetch carries its own
        ``select_related`` so the identities arrive with the links instead of in
        a second round trip. Three queries per page, whatever the page size.
        """
        from vinta_audit_logs.models import AuditAffectedIdentity

        return qs.select_related("scope", "actor", "on_behalf_of").prefetch_related(
            Prefetch(
                "affected_links",
                queryset=AuditAffectedIdentity.objects.select_related("identity"),
            )
        )

    def _filtered_queryset(self, q: AuditQuery):
        """Translate an AuditQuery into a filtered (unordered) queryset.

        Every list-valued field becomes an ``IN`` and is applied only when it is
        not None -- so ``[]`` reaches the database as ``IN ()`` and matches
        nothing, which is the documented meaning. The composite filters
        (``scopes``, ``actors``, ``subjects``) become an OR of equality pairs,
        because the identity they match is a pair rather than a column.

        The scope, action and subject filters all hit the denormalized columns
        on ``Audit`` rather than the foreign keys, so the browse indexes apply
        and no join is needed to satisfy them.
        """
        from vinta_audit_logs.models import Audit

        qs = Audit.objects.all()

        if q.scope_keys is not None:
            qs = qs.filter(scope_key__in=q.scope_keys)

        if q.scope_types is not None:
            qs = qs.filter(scope_type__in=q.scope_types)

        if q.scopes is not None:
            qs = qs.filter(
                _or_group(
                    Q(scope_type=scope.scope_type, scope_key=scope.scope_key) for scope in q.scopes
                )
            )

        if q.uids is not None:
            qs = qs.filter(uid__in=q.uids)

        if q.actions is not None:
            qs = qs.filter(action_key__in=q.actions)

        if q.actor_types is not None:
            qs = qs.filter(actor_type__in=q.actor_types)

        if q.actors is not None:
            qs = qs.filter(
                _or_group(
                    Q(actor_type=actor.identity_type, actor_key=actor.identity_key)
                    for actor in q.actors
                )
            )

        if q.subject_types is not None:
            qs = qs.filter(subject_content_type_key__in=q.subject_types)

        if q.subjects is not None:
            qs = qs.filter(
                _or_group(
                    Q(
                        subject_content_type_key=subject.subject_type,
                        subject_pk=subject.subject_id,
                    )
                    for subject in q.subjects
                )
            )

        if q.affected is not None:
            # Join via the through table's reverse relation. distinct() because
            # a record affecting two of the named identities would otherwise
            # come back twice -- once per matching through row.
            qs = qs.filter(
                _or_group(
                    Q(
                        affected_links__identity_type=identity.identity_type,
                        affected_links__identity_key=identity.identity_key,
                    )
                    for identity in q.affected
                )
            ).distinct()

        if q.created_after is not None:
            qs = qs.filter(created_at__gte=q.created_after)

        if q.created_before is not None:
            qs = qs.filter(created_at__lt=q.created_before)

        if q.has_diff is not None:
            # Relies on the diff invariant enforced by bulk_add(): diff is None
            # or a NON-EMPTY dict; empty dicts are normalized to None at write
            # time.
            qs = qs.filter(diff__isnull=not q.has_diff)

        if q.search is not None:
            term = q.search
            qs = qs.filter(
                Q(subject_content_type_key__icontains=term)
                | Q(subject_pk__icontains=term)
                | Q(subject_label__icontains=term)
                | Q(actor_key__icontains=term)
                | Q(actor__identity_label__icontains=term)
            )

        return qs

    def _to_record(self, audit) -> AuditRecord:
        """Map an Audit row to the portable AuditRecord DTO.

        Expects ``_with_related`` to have been applied; without it this triggers
        a query per related row.
        """
        return AuditRecord(
            id=audit.pk,
            uid=audit.uid,
            created_at=audit.created_at,
            action_key=audit.action_key,
            scope=self.scope_to_ref(audit.scope),
            actor=self.identity_to_snapshot(audit.actor),
            on_behalf_of=(
                self.identity_to_snapshot(audit.on_behalf_of)
                if audit.on_behalf_of_id is not None
                else None
            ),
            subject=SubjectRef(
                subject_type=audit.subject_content_type_key,
                subject_id=audit.subject_pk,
                subject_label=audit.subject_label,
            ),
            # Sorted for stable comparisons in tests and callers.
            affected=sorted(
                (self.identity_to_snapshot(link.identity) for link in audit.affected_links.all()),
                key=lambda snapshot: (snapshot.identity_type, snapshot.identity_key),
            ),
            diff=audit.diff,
        )


class InMemoryAuditRepository(AuditRepository):
    """Process-local AuditRepository, backed by a dict keyed on ``uid``.

    Exists for two reasons, both about the multi-repository design rather than
    about convenience:

    * It is the **second implementation** the interface was designed for. A
      replication or sync path that has only ever run ORM-to-ORM proves very
      little; wiring this one in as an additional repository exercises the parts
      that must not assume the ORM -- portable identity, portable filtering, an
      id space that is not the ORM's.
    * It is the reference for a real non-ORM backend. Every read goes through
      ``vinta_audit_logs.filtering.apply_query``, so the filter semantics come
      out identical to the ORM repository's for free.

    Not durable and not shared between processes. Use it in tests and in local
    development, never as somewhere production audit records are expected to
    survive.
    """

    def __init__(self) -> None:
        # Keyed on uid, which is what makes a write an upsert: the same record
        # written twice replaces its entry instead of adding one.
        self._records: dict[uuid.UUID, AuditRecord] = {}
        # Stands in for an autoincrement primary key, so `id` is this
        # repository's own and does not accidentally match the ORM's.
        self._next_id = 1

    def add(self, data: AuditRecordData) -> AuditRecord:
        """Upsert one record. See :meth:`bulk_add`."""
        return self.bulk_add([data])[0]

    def bulk_add(self, data: Sequence[AuditRecordData]) -> list[AuditRecord]:
        """Upsert a batch of records, keyed on uid.

        A record whose uid is already present keeps the id it was first given
        (an id is stable within a repository) and has every other field
        rewritten from the incoming data.
        """
        now = timezone.now()
        results = []
        for item in data:
            existing = self._records.get(item.uid)
            if existing is None:
                record_id = self._next_id
                self._next_id += 1
            else:
                record_id = existing.id
            record = AuditRecord(
                id=record_id,
                uid=item.uid,
                created_at=item.created_at or now,
                action_key=item.action_key,
                action_name=item.action_name,
                action_content_type_key=item.action_content_type_key,
                scope=item.scope,
                actor=item.actor,
                on_behalf_of=item.on_behalf_of,
                subject=item.subject,
                affected=sorted(
                    _dedupe_snapshots(item.affected),
                    key=lambda snapshot: (snapshot.identity_type, snapshot.identity_key),
                ),
                diff=item.diff or None,
            )
            self._records[item.uid] = record
            results.append(record)
        return results

    def get(self, audit_id: int) -> AuditRecord | None:
        """Retrieve by this repository's own id."""
        return next(
            (record for record in self._records.values() if record.id == audit_id),
            None,
        )

    def get_by_uid(self, uid: uuid.UUID) -> AuditRecord | None:
        """Retrieve by cross-repository identity -- a dict lookup here."""
        return self._records.get(uid)

    def query(
        self,
        q: AuditQuery,
        *,
        offset: int = 0,
        limit: int = 50,
        ordering: str | Sequence[str] = DEFAULT_ORDERING,
    ) -> AuditPage:
        """Filter, order and paginate via the shared pure-Python implementation."""
        return apply_query(
            self._records.values(), q, offset=offset, limit=limit, ordering=ordering
        )
