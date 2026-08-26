"""How a recorded entry reaches the database, and what happens when it cannot."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.test import override_settings

from tests.conftest import scoped
from vinta_audit_logs.dispatch import get_dispatcher
from vinta_audit_logs.models import Audit
from vinta_audit_logs.types import AuditQuery, SubjectRef

pytestmark = pytest.mark.django_db


def test_a_record_is_written_after_the_transaction_commits(
    service, django_capture_on_commit_callbacks
):
    """The inline dispatcher still waits for commit.

    An audit record describing an action that rolled back is worse than no
    record, so the wait is the service's job rather than each dispatcher's.
    """
    with django_capture_on_commit_callbacks(execute=True):
        service.record(
            action="create",
            actor=service.system_identity(),
            subject=SubjectRef(subject_type="testapp.article", subject_id="1"),
            scope=scoped("1"),
        )

    assert Audit.objects.count() == 1


def test_nothing_is_written_before_commit(service, django_capture_on_commit_callbacks):
    """And nothing lands early."""
    with django_capture_on_commit_callbacks(execute=False):
        service.record(
            action="create",
            actor=service.system_identity(),
            subject=SubjectRef(subject_type="testapp.article", subject_id="1"),
            scope=scoped("1"),
        )
        assert Audit.objects.count() == 0


def test_a_failing_dispatcher_does_not_break_the_action(
    service, django_capture_on_commit_callbacks, caplog
):
    """The audited action must survive its own instrumentation.

    A broker outage, a serialization problem, a misconfigured dispatcher -- none
    of them may propagate into the business operation that emitted the record.
    """
    with (
        patch.object(service, "get_dispatcher", side_effect=RuntimeError("broker down")),
        django_capture_on_commit_callbacks(execute=True),
    ):
        service.record(
            action="create",
            actor=service.system_identity(),
            subject=SubjectRef(subject_type="testapp.article", subject_id="1"),
            scope=scoped("1"),
        )

    assert Audit.objects.count() == 0
    assert "Failed to dispatch audit record" in caplog.text


@override_settings(AUDIT_RECORD_DISPATCHER="vinta_audit_logs.dispatch.dispatch_via_celery")
def test_the_celery_dispatcher_is_the_default_shape():
    """``AUDIT_RECORD_DISPATCHER`` selects the dispatcher by dotted path."""
    from vinta_audit_logs.dispatch import dispatch_via_celery

    assert get_dispatcher() is dispatch_via_celery


@override_settings(AUDIT_SERVICE_FACTORY=None)
def test_a_missing_service_factory_is_logged_not_raised(caplog):
    """A misconfiguration loses the record loudly rather than crashing a worker."""
    from vinta_audit_logs.services import resolve_audit_service

    assert resolve_audit_service() is None
    assert "AUDIT_SERVICE_FACTORY" in caplog.text


def test_records_are_readable_through_the_service(service, django_capture_on_commit_callbacks):
    """Round trip: record, then read it back through the same service."""
    with django_capture_on_commit_callbacks(execute=True):
        service.record(
            action="update",
            actor=service.system_identity(),
            subject=SubjectRef(subject_type="testapp.article", subject_id="42"),
            scope=scoped("1"),
            diff={"title": {"old": "a", "new": "b"}},
        )

    page = service.query(AuditQuery(scope_keys=["1"]))
    assert page.total == 1
    assert page.items[0].action_key == "update"
    assert page.items[0].diff == {"title": {"old": "a", "new": "b"}}
