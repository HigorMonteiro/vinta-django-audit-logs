"""AuditService -- the write and read entry point for the audit trail.

Usage, from a caller holding a service:

    self.audit_service.record(
        scope=ScopeRef(scope_type=ScopeType.SCOPED, scope_key=str(org.pk)),
        action=AuditActionKey.UPDATE,
        actor=self.audit_service.identity_from_user(user),
        subject=self.audit_service.subject_from_instance(membership),
        diff=diff,
    )

Callers must NOT call ``record()`` from inside the background task that is
already the async persistence boundary -- that is ``persist_audit_record``'s job.

Subclassing
-----------
This class knows how to write an audit record; it does not know what a *scope*
or an *actor* is in your project. Those are the hooks:

* :meth:`identity_from_user` / :meth:`system_identity` -- build the actor
  snapshot, synchronously, from whatever principal the project authenticates.
* :meth:`scope_from` -- turn a project object (an organization, a workspace)
  into a portable ``ScopeRef``.

A project subclasses this, overrides those, and adds whatever additional
builders its call sites want. The row-writing half of the same seam lives on
``DjangoORMAuditRepository`` (``build_scope_defaults`` / ``build_identity_defaults``);
the two are separate because one runs in the request and one runs in the worker.

Multiple repositories
---------------------
The service holds one **main** repository and any number of **additional** ones,
each under a caller-chosen alias:

    AuditService(repository=orm_repo, additional_repositories={"warehouse": ...})

Every record is written to the main repository first; that write is the one the
audit trail's durability rests on. It is then *tentatively* replicated to each
additional repository -- best effort, one at a time, and a failure is logged and
swallowed rather than allowed to unwind the main write. A replica that misses
records is expected to happen and is repaired by ``sync_repository``, not by
failing the action that emitted the record.

Reads take a repository alias, so a caller can ask the same question of any of
them. Nothing here duplicates records across repositories: every record carries
a ``uid`` generated once at emit time, and every repository write is an upsert
on it, so replicating or re-syncing a record the target already holds converges
on the row it has.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils import timezone
from django.utils.module_loading import import_string

from vinta_audit_logs import conf
from vinta_audit_logs.constants import IdentityType, ScopeType
from vinta_audit_logs.exceptions import UnknownAuditRepositoryError
from vinta_audit_logs.filtering import DEFAULT_ORDERING
from vinta_audit_logs.repositories import (
    DEFAULT_ITERATION_CHUNK_SIZE,
    AuditRepository,
)
from vinta_audit_logs.serialization import serialize_record_data
from vinta_audit_logs.types import (
    AuditPage,
    AuditQuery,
    AuditRecord,
    AuditRecordData,
    AuditSyncResult,
    IdentitySnapshot,
    ScopeRef,
    SubjectRef,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from django.db.models import Model


logger = logging.getLogger(__name__)


def resolve_audit_service() -> AuditService | None:
    """Build the service that persists records, from ``AUDIT_SERVICE_FACTORY``.

    The worker has no request to carry a service, and this app cannot construct
    one for a project -- it does not know which repositories that project runs,
    nor how it wires its dependencies. So the project names a zero-argument
    callable and this calls it.

    Returns None rather than raising: the caller is a background task or a
    fire-and-forget dispatcher, and a misconfiguration should be a loud log line
    rather than a crashed worker.
    """
    path = conf.get(conf.AUDIT_SERVICE_FACTORY)
    if not path:
        logger.error(
            "%s is not set, so no AuditService can be built and no audit record "
            "will be persisted. Point it at a zero-argument callable returning a "
            "configured AuditService.",
            conf.AUDIT_SERVICE_FACTORY,
        )
        return None
    try:
        return import_string(path)()
    except Exception:
        logger.exception(
            "%s (%r) could not produce an AuditService.", conf.AUDIT_SERVICE_FACTORY, path
        )
        return None


#: Alias of the main repository. Reserved: an entry under this key in
#: ``additional_repositories`` would make ``repository="main"`` ambiguous, so
#: the constructor drops it rather than letting the two disagree.
MAIN_REPOSITORY_ALIAS = "main"

#: Records handed to a target repository per ``bulk_add`` during a sync.
DEFAULT_SYNC_BATCH_SIZE = 500


class AuditService:
    """Records audit trail entries asynchronously, and reads them back.

    Actor context is captured synchronously at call time and serialized into the
    task payload so the worker never re-reads mutable state that may have
    changed or been deleted by the time it runs.
    """

    def __init__(
        self,
        repository: AuditRepository,
        additional_repositories: Mapping[str, AuditRepository] | None = None,
    ) -> None:
        self.repository = repository
        # Copied, so a container-held mapping cannot be mutated through the
        # service, and stripped of MAIN_REPOSITORY_ALIAS, which the main
        # repository owns.
        self.additional_repositories: dict[str, AuditRepository] = {
            alias: repo
            for alias, repo in (additional_repositories or {}).items()
            if alias != MAIN_REPOSITORY_ALIAS
        }

    # ------------------------------------------------------------------
    # Repository selection
    # ------------------------------------------------------------------

    @property
    def repository_aliases(self) -> tuple[str, ...]:
        """Every alias :meth:`get_repository` accepts, main first."""
        return (MAIN_REPOSITORY_ALIAS, *self.additional_repositories)

    def get_repository(self, repository: str | None = None) -> AuditRepository:
        """Resolve a repository alias to the repository itself.

        Args:
            repository: An alias, or None / ``"main"`` for the main repository.

        Returns:
            The named repository.

        Raises:
            UnknownAuditRepositoryError: The alias is not configured. Deliberately
                not a silent fallback to the main repository: answering a
                question about one store with another store's data is worse than
                failing.
        """
        if repository is None or repository == MAIN_REPOSITORY_ALIAS:
            return self.repository
        try:
            return self.additional_repositories[repository]
        except KeyError:
            raise UnknownAuditRepositoryError(repository, self.repository_aliases) from None

    # ------------------------------------------------------------------
    # Hooks: building portable references, SYNCHRONOUSLY
    # ------------------------------------------------------------------

    def scope_from(self, obj: Any) -> ScopeRef:
        """Turn a project object into the scope a record belongs to.

        The default handles the two cases this app can reason about on its own:
        ``None`` is the global scope, and a Django model instance is a scope
        keyed on its primary key. A project with a real tenant boundary
        overrides this to key on whatever it uses.

        Args:
            obj: The scope-bearing object, or None for a global record.

        Returns:
            A portable ScopeRef.
        """
        if obj is None:
            return ScopeRef.global_scope()
        return ScopeRef(
            scope_type=ScopeType.SCOPED,
            scope_key=str(getattr(obj, "pk", obj)),
        )

    def identity_from_user(self, user: Any) -> IdentitySnapshot:
        """Snapshot a Django user as the actor of a record.

        Captures groups and permissions **now**, in the request, because both
        are mutable state the worker must not re-read: an audit trail that looks
        up the actor's groups at write time records the wrong groups.

        Args:
            user: A user instance, or None for the system actor.

        Returns:
            A portable IdentitySnapshot.
        """
        if user is None:
            return self.system_identity()
        return IdentitySnapshot(
            identity_type=IdentityType.USER,
            identity_key=str(user.pk),
            identity_label=self.label_for_user(user),
            user_id=user.pk,
            is_staff=bool(getattr(user, "is_staff", False)),
            is_superuser=bool(getattr(user, "is_superuser", False)),
            group_names=sorted(user.groups.values_list("name", flat=True)),
            permission_keys=sorted(user.get_all_permissions()),
        )

    def label_for_user(self, user: Any) -> str:
        """Human-readable name for a user, captured at emit time.

        Reads ``get_username()`` and nothing else. Deliberately NOT ``str(user)``
        or ``get_full_name()``: both routinely reach through a relation -- a
        profile row, an employer, a preferred-name table -- and any query they
        run can raise inside the business action being audited. The username is
        a column on the user itself.

        Even that is wrapped, because a project is free to override the method
        with something that is not cheap. A missing label costs a slightly less
        readable audit row; an exception here would cost the action.
        """
        try:
            getter = getattr(user, "get_username", None)
            value = getter() if callable(getter) else None
            if value:
                return str(value)
        except Exception:
            # get_username() can raise is worth failing the audited action for.
            logger.warning(
                "Could not read a display name for the audit actor; recording "
                "the record without one.",
                exc_info=True,
            )
        return ""

    def system_identity(self, label: str = "system") -> IdentitySnapshot:
        """The actor for an action nothing and nobody was behind.

        A cron job, a data migration, a startup task. Carries no user and no id.
        """
        return IdentitySnapshot(
            identity_type=IdentityType.SYSTEM,
            identity_key="",
            identity_label=label,
        )

    def subject_from_instance(self, instance: Model, label: str = "") -> SubjectRef:
        """Build a SubjectRef from a Django model instance.

        Derives ``subject_type`` as ``"app_label.modelname"`` and ``subject_id``
        from the instance pk, so call sites don't repeat the soft-reference
        shape.

        ``subject_label`` is left empty unless a caller passes one. Deliberately
        NOT defaulted to ``str(instance)``: a model ``__str__`` can dereference
        related rows and raise, and building the audit payload must never break
        the business action it describes. Pass a cheap label explicitly (a name
        already in memory) when a human-readable one is worthwhile.

        Args:
            instance: A Django model instance.
            label: Optional human-readable label; not auto-computed.

        Returns:
            A SubjectRef referencing the instance.
        """
        meta = instance._meta
        return SubjectRef(
            subject_type=f"{meta.app_label}.{meta.model_name}",
            subject_id=str(instance.pk),
            subject_label=label,
        )

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        action: str,
        actor: IdentitySnapshot,
        subject: SubjectRef,
        scope: ScopeRef | None = None,
        on_behalf_of: IdentitySnapshot | None = None,
        affected: Sequence[IdentitySnapshot] = (),
        diff: dict | None = None,
        action_name: str = "",
        action_content_type_key: str = "",
    ) -> None:
        """Record an audit trail entry asynchronously.

        Builds an ``AuditRecordData``, serializes it to a JSON-safe dict, and
        enqueues ``persist_audit_record``. The task runs the repository write out
        of band so a slow or failing write never blocks the caller.

        Enqueue errors (broker unavailability, serialization problems) are
        caught, logged and swallowed so the business action that triggered the
        audit record is never affected. Repository errors happen in the worker
        and are therefore already off the caller's critical path.

        Args:
            action: The action key. Resolved against ``AuditAction`` in the
                worker, so an unknown key is created rather than rejected --
                pass ``action_name`` to give a new one a label.
            actor: Pre-built IdentitySnapshot; must be built synchronously,
                before any async boundary.
            subject: The subject reference for the audited object.
            scope: What the record belongs to. None means the global scope.
            on_behalf_of: The identity behind the actor, when one principal
                acted as another.
            affected: Identities this action affected, as distinct from the one
                that took it.
            diff: ``{field: {"old": ..., "new": ...}}``. Pass None (or omit)
                when there is no diff; an empty dict is treated the same way.
            action_name: Display label, used only if the action row is created.
            action_content_type_key: ``"app_label.modelname"`` the action applies
                to, again used only if the action row is created.
        """
        data = AuditRecordData(
            action_key=str(action),
            actor=actor,
            subject=subject,
            scope=scope if scope is not None else ScopeRef.global_scope(),
            on_behalf_of=on_behalf_of,
            affected=list(affected),
            diff=diff or None,
            action_name=action_name,
            action_content_type_key=action_content_type_key,
            # Both identity and emit time are fixed HERE, synchronously, for the
            # same reason the actor snapshot is: they must describe the action,
            # not the worker that eventually writes it. The uid additionally
            # makes the write idempotent -- a task can run twice, and with a uid
            # the second run upserts the record the first one wrote instead of
            # appending a duplicate.
            uid=uuid.uuid7(),
            created_at=timezone.now(),
        )

        payload = self.serialize(data)

        def _dispatch() -> None:
            try:
                self.get_dispatcher()(payload)
            except Exception:
                logger.exception(
                    "Failed to dispatch audit record for action %r in scope %r. "
                    "The record will not be persisted.",
                    data.action_key,
                    data.scope.scope_key,
                )

        # After commit, always. An audit record describing an action that rolled
        # back is worse than no record, and this is the one place that holds for
        # every dispatcher.
        transaction.on_commit(_dispatch)

    def get_dispatcher(self):
        """The callable that arranges for a dispatched record to be persisted.

        Resolved per call rather than held on the instance so a test can swap
        ``AUDIT_RECORD_DISPATCHER`` with ``override_settings``. See
        ``vinta_audit_logs.dispatch``.
        """
        from vinta_audit_logs.dispatch import get_dispatcher

        return get_dispatcher()

    @staticmethod
    def serialize(data: AuditRecordData) -> dict:
        """Reduce a record to the JSON-safe dict a dispatcher carries.

        Delegates to ``vinta_audit_logs.serialization``, which owns both
        directions of the conversion and knows nothing about Celery.
        """
        return serialize_record_data(data)

    def persist(self, data: AuditRecordData) -> AuditRecord:
        """Write one record to the main repository, then replicate it.

        The persistence boundary itself: called by ``persist_audit_record`` once
        the record has crossed into the worker. Splitting it from ``record()``
        keeps the "which repositories does a record go to" policy in the service
        rather than in the task.

        The main write is allowed to raise -- the task above it decides what a
        failed audit write means. Replication is not: see :meth:`replicate`.

        Args:
            data: The record to persist.

        Returns:
            The record as stored in the main repository.
        """
        record = self.repository.add(data)
        self.replicate(record)
        return record

    def replicate(
        self, record: AuditRecord, *, targets: Sequence[str] | None = None
    ) -> dict[str, bool]:
        """Copy an already-persisted record into the additional repositories.

        Tentative by design. Each target is written independently and a failure
        is logged and swallowed, because the record is already durable in the
        main repository and an unreachable replica must not be able to fail --
        or, worse, roll back -- the write that succeeded. Reconciling what a
        replica missed is :meth:`sync_repository`'s job.

        The write is an upsert on ``record.uid``, so replicating a record a
        target already holds is a no-op rather than a duplicate.

        Args:
            record: The record as returned by the main repository, carrying the
                uid and created_at every copy must share.
            targets: Aliases to replicate to; defaults to every additional
                repository. Unknown aliases are reported as failures rather than
                raised, so one bad name cannot stop the others.

        Returns:
            Per-target success flags, keyed by alias.
        """
        aliases = tuple(targets) if targets is not None else tuple(self.additional_repositories)
        data = record.to_data()
        results: dict[str, bool] = {}
        for alias in aliases:
            try:
                self.get_repository(alias).bulk_add([data])
            except Exception:
                logger.exception(
                    "Failed to replicate audit record %s (action %r, scope %r) to "
                    "repository %r. The main repository still holds it; run "
                    "AuditService.sync_repository(%r) to reconcile.",
                    record.uid,
                    record.action_key,
                    record.scope.scope_key,
                    alias,
                    alias,
                )
                results[alias] = False
            else:
                results[alias] = True
        return results

    # ------------------------------------------------------------------
    # Read path -- every method takes the repository to read from
    # ------------------------------------------------------------------

    def get(self, audit_id: int, *, repository: str | None = None) -> AuditRecord | None:
        """Fetch a record by the named repository's own id.

        ``audit_id`` is backend-local: the same record has a different id in each
        repository. Reach for :meth:`get_by_uid` when the id has to mean the same
        thing in more than one of them.
        """
        return self.get_repository(repository).get(audit_id)

    def get_by_uid(self, uid: uuid.UUID, *, repository: str | None = None) -> AuditRecord | None:
        """Fetch a record by its cross-repository identity."""
        return self.get_repository(repository).get_by_uid(uid)

    def query(
        self,
        q: AuditQuery,
        *,
        offset: int = 0,
        limit: int = 50,
        ordering: str | Sequence[str] = DEFAULT_ORDERING,
        repository: str | None = None,
    ) -> AuditPage:
        """Run a filter against the named repository.

        The same ``AuditQuery`` means the same thing whichever repository it is
        pointed at -- that contract lives in ``vinta_audit_logs.filtering``.
        """
        return self.get_repository(repository).query(
            q, offset=offset, limit=limit, ordering=ordering
        )

    def count(self, q: AuditQuery, *, repository: str | None = None) -> int:
        """Count the records matching ``q`` in the named repository.

        Comparing this across two aliases is the cheap way to see how far a
        replica has drifted from the main repository.
        """
        return self.get_repository(repository).count(q)

    def iter_records(
        self,
        q: AuditQuery,
        *,
        chunk_size: int = DEFAULT_ITERATION_CHUNK_SIZE,
        repository: str | None = None,
    ) -> Iterator[AuditRecord]:
        """Stream every record matching ``q`` from the named repository.

        Oldest first, in bounded memory. Use it to export or reconcile a whole
        log without paginating by hand.
        """
        yield from self.get_repository(repository).iter_records(q, chunk_size=chunk_size)

    # ------------------------------------------------------------------
    # Sync / backfill
    # ------------------------------------------------------------------

    def sync_repository(
        self,
        target: str,
        *,
        source: str | None = None,
        query: AuditQuery | None = None,
        batch_size: int = DEFAULT_SYNC_BATCH_SIZE,
    ) -> AuditSyncResult:
        """Backfill one repository from another, batch by batch.

        The repair path for everything replication is allowed to lose: a target
        that was unreachable when a record was emitted, one added after the log
        already existed, one that fell behind during an incident.

        Safe to re-run and safe to overlap with live replication, because every
        write is an upsert on the record's ``uid``. Syncing a window that is
        already in step rewrites those records with identical content rather than
        duplicating them, so the conservative move -- sync a wider window than you
        think you need -- costs time and nothing else.

        Reads stream through ``iter_records`` (oldest first, bounded memory), so
        the whole log never has to fit in memory however large it is. A batch that
        fails is counted, its error recorded, and the walk continues: one bad
        chunk cannot strand every record behind it. Inspect ``AuditSyncResult.ok``
        to find out whether a re-run is needed.

        Args:
            target: Alias of the repository to write into.
            source: Alias of the repository to read from; None means main.
            query: Restrict the sync to part of the log -- most usefully by
                ``scope_keys`` or a ``created_after`` / ``created_before`` window.
                None syncs everything the source holds.
            batch_size: Records per ``bulk_add`` against the target.

        Returns:
            An AuditSyncResult with per-run counts and any batch errors.

        Raises:
            UnknownAuditRepositoryError: Either alias is not configured.
            ValueError: ``target`` names the same repository as ``source``, which
                would rewrite a log with itself.
        """
        source_alias = source or MAIN_REPOSITORY_ALIAS
        source_repository = self.get_repository(source_alias)
        target_repository = self.get_repository(target)
        if source_repository is target_repository:
            raise ValueError(
                f"Cannot sync audit repository {target!r} from itself "
                f"(source {source_alias!r} resolves to the same repository)."
            )

        # Counters rather than a rebuilt AuditSyncResult per record: the result
        # is frozen, and a dataclasses.replace for every one of a million records
        # is a million allocations to produce one integer.
        read = 0
        written = 0
        failed = 0
        errors: list[str] = []
        batch: list[AuditRecordData] = []

        def flush() -> None:
            # `batch` is rebound rather than cleared in place: the target keeps a
            # reference to the list it was handed, and clearing would empty that
            # too.
            nonlocal batch, written, failed
            if not batch:
                return
            try:
                target_repository.bulk_add(batch)
            except Exception as exc:
                logger.exception(
                    "Audit sync %s -> %s: batch of %d records failed.",
                    source_alias,
                    target,
                    len(batch),
                )
                failed += len(batch)
                errors.append(f"{type(exc).__name__}: {exc}")
            else:
                written += len(batch)
            batch = []

        for record in source_repository.iter_records(query or AuditQuery(), chunk_size=batch_size):
            read += 1
            batch.append(record.to_data())
            if len(batch) >= batch_size:
                flush()
        flush()

        logger.info(
            "Audit sync %s -> %s finished: read=%d written=%d failed=%d.",
            source_alias,
            target,
            read,
            written,
            failed,
        )
        return AuditSyncResult(
            source=source_alias,
            target=target,
            read=read,
            written=written,
            failed=failed,
            errors=errors,
        )

    def sync_all_repositories(
        self,
        *,
        source: str | None = None,
        query: AuditQuery | None = None,
        batch_size: int = DEFAULT_SYNC_BATCH_SIZE,
    ) -> dict[str, AuditSyncResult]:
        """Backfill every additional repository from ``source``.

        A convenience over :meth:`sync_repository`; the source repository is
        skipped if it happens to be one of the additional ones.
        """
        source_alias = source or MAIN_REPOSITORY_ALIAS
        return {
            alias: self.sync_repository(
                alias, source=source_alias, query=query, batch_size=batch_size
            )
            for alias in self.additional_repositories
            if alias != source_alias
        }
