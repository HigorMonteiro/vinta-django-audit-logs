"""How a recorded audit entry gets from the request to the database.

``AuditService.record`` builds a portable, JSON-safe payload and hands it to a
*dispatcher*. What the dispatcher does with it is the one part of the write path
that genuinely differs between projects: most queue it, some write it inline,
and a few will want to put it on a stream.

Two ship here. ``AUDIT_RECORD_DISPATCHER`` names the one in use; the default is
Celery, because a queue is where an audit write belongs -- it must not be able to
slow down or fail the action it describes.

Whichever runs, it runs inside ``transaction.on_commit``: an audit record for an
action that rolled back is a lie, and this is the only place that can be
enforced once for every dispatcher.
"""

from __future__ import annotations

import logging

from django.utils.module_loading import import_string

from vinta_audit_logs import conf

logger = logging.getLogger(__name__)


def get_dispatcher():
    """Resolve the configured dispatcher.

    Returns:
        A callable taking one JSON-safe payload dict.
    """
    return import_string(conf.get(conf.AUDIT_RECORD_DISPATCHER, conf.DEFAULT_RECORD_DISPATCHER))


def dispatch_via_celery(payload: dict) -> None:
    """Hand the record to the Celery task that persists it.

    The default. Needs ``AUDIT_CELERY_APP`` pointing at the project's Celery
    application -- an installable app cannot own one -- and the import is
    deferred to here so a project using a different dispatcher never needs
    Celery installed at all.
    """
    from vinta_audit_logs.tasks import persist_audit_record

    persist_audit_record.delay(payload)


def dispatch_inline(payload: dict) -> None:
    """Persist the record in this process, right now.

    For a project with no task queue, and for tests. Still runs after commit --
    the service arranges that -- so it cannot record an action that rolled back.

    Slower and less resilient than the queue: the write happens in the request,
    and a database hiccup is logged and the record lost rather than retried.
    That is the trade a project makes by choosing this, and it is a reasonable
    one at low volume.
    """
    from vinta_audit_logs.serialization import deserialize_record_data
    from vinta_audit_logs.services import resolve_audit_service

    service = resolve_audit_service()
    if service is None:
        logger.error(
            "dispatch_inline: no AuditService available; the audit record will "
            "not be persisted. Payload: %r",
            payload,
        )
        return
    try:
        service.persist(deserialize_record_data(payload))
    except Exception:
        logger.exception(
            "dispatch_inline: could not persist the audit record for action %r.",
            payload.get("action_key"),
        )
