"""Background tasks: the async persistence boundary and the sync entry point.

The write path: ``AuditService.record()`` enqueues ``persist_audit_record`` with
a JSON-safe dict payload; this task rebuilds the ``AuditRecordData`` and hands it
to ``AuditService.persist()``, which writes the main repository and then
tentatively replicates to the additional ones.

Tasks are configured with late acknowledgement in most deployments, which means
they must be idempotent. They are: every record carries a ``uid`` generated once
at emit time, and every repository write is an upsert on that uid, so a re-run
rewrites the record the first run wrote instead of appending a second copy.

Task failures are logged and swallowed rather than re-raised, so a bad payload
does not crash the worker. The record is lost in that case, which is the trade a
fire-and-forget audit trail makes -- and the reason ``AuditService.record``
validates what it can *synchronously*, in the request, where an exception is
attributable to the call site that caused it.

**Importing this module requires Celery.** That is deliberate and it is why
nothing else in the package imports it at module scope: a project dispatching
records some other way (see ``vinta_audit_logs.dispatch``) never touches this
file and never needs Celery installed. The Celery application itself belongs to
the project, not to an installable app, so it is named by dotted path through
``AUDIT_CELERY_APP``.
"""

from __future__ import annotations

import logging
from datetime import datetime

from django.utils.module_loading import import_string

from vinta_audit_logs import conf
from vinta_audit_logs.serialization import deserialize_record_data
from vinta_audit_logs.services import resolve_audit_service
from vinta_audit_logs.types import AuditQuery

logger = logging.getLogger(__name__)


def _celery_app():
    """Resolve the project's Celery app from ``AUDIT_CELERY_APP``.

    A package cannot own a Celery application -- the project does -- so the
    setting names one by dotted path, the way ``CELERY_BEAT_SCHEDULE`` entries
    name tasks.
    """
    return import_string(
        conf.require(
            conf.AUDIT_CELERY_APP,
            "Point it at the project's Celery application by dotted path (for "
            "example 'myproject.celery.app') so vinta_audit_logs can register its "
            "tasks on it -- or set AUDIT_RECORD_DISPATCHER to a dispatcher that "
            "does not use Celery.",
        )
    )


app = _celery_app()


@app.task
def persist_audit_record(payload: dict) -> None:
    """Persist a single audit record via the audit service.

    Reconstructs an AuditRecordData from the JSON payload produced by
    ``AuditService.record()`` and calls ``persist()``, which writes the main
    repository and then replicates, best effort, to the additional ones.
    Failures are logged and swallowed so the worker stays alive even when given
    a malformed payload or when the database is temporarily unavailable.

    Args:
        payload: A JSON-safe dict produced by ``AuditService.serialize()``.
    """
    audit_service = resolve_audit_service()
    if audit_service is None:
        logger.error(
            "persist_audit_record: no AuditService available; audit record will "
            "not be persisted. Payload: %r",
            payload,
        )
        return

    try:
        data = deserialize_record_data(payload)
    except Exception:
        logger.exception(
            "persist_audit_record: malformed payload, cannot reconstruct "
            "AuditRecordData. Payload: %r",
            payload,
        )
        return

    try:
        audit_service.persist(data)
    except Exception:
        logger.exception(
            "persist_audit_record: persist() failed for action %r in scope %r.",
            payload.get("action_key"),
            (payload.get("scope") or {}).get("scope_key"),
        )


@app.task
def sync_audit_repository(
    target: str,
    *,
    source: str | None = None,
    scope_keys: list[str] | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    batch_size: int = 500,
) -> dict | None:
    """Backfill one additional audit repository from another, out of band.

    The operational entry point for the reconciliation that
    ``AuditService.replicate`` deliberately does not do: filling in whatever a
    replica missed while it was unreachable, or loading a newly-added repository
    with the history that predates it. A backfill walks the whole window it is
    given, so it belongs in a worker rather than in a request.

    Re-running it is safe. Every write is an upsert on the record's uid, so a
    window already in step is rewritten with identical content rather than
    duplicated -- which also makes it safe to run while live replication
    continues.

    The window arrives as ISO 8601 strings rather than datetimes because the
    task serializer is JSON.

    Args:
        target: Alias of the repository to write into.
        source: Alias of the repository to read from; None means main.
        scope_keys: Restrict the sync to these scopes.
        created_after: ISO 8601 lower bound on created_at, inclusive.
        created_before: ISO 8601 upper bound on created_at, exclusive.
        batch_size: Records per bulk_add against the target.

    Returns:
        The AuditSyncResult as a dict (JSON-safe, so it can be a task result),
        or None when the sync could not be started.
    """
    audit_service = resolve_audit_service()
    if audit_service is None:
        logger.error(
            "sync_audit_repository: no AuditService available; sync to %r will not run.",
            target,
        )
        return None

    try:
        query = AuditQuery(
            scope_keys=scope_keys,
            created_after=datetime.fromisoformat(created_after) if created_after else None,
            created_before=datetime.fromisoformat(created_before) if created_before else None,
        )
    except ValueError:
        logger.exception(
            "sync_audit_repository: unparseable window (created_after=%r, "
            "created_before=%r); sync to %r will not run.",
            created_after,
            created_before,
            target,
        )
        return None

    try:
        result = audit_service.sync_repository(
            target, source=source, query=query, batch_size=batch_size
        )
    except Exception:
        logger.exception("sync_audit_repository: sync to %r failed to run.", target)
        return None

    return {
        "source": result.source,
        "target": result.target,
        "read": result.read,
        "written": result.written,
        "failed": result.failed,
        "errors": result.errors,
    }


#: Re-exported so ``from vinta_audit_logs.tasks import deserialize_record_data``
#: keeps working; it lives in ``serialization`` now because rebuilding a record
#: from JSON has nothing to do with Celery.
__all__ = ["app", "deserialize_record_data", "persist_audit_record", "sync_audit_repository"]
