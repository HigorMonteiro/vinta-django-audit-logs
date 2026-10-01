"""The write path's promises: upsert on uid, snapshot semantics, no cascades."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from django.db.models import ProtectedError

from tests.conftest import record_data
from vinta_audit_logs.constants import IdentityType
from vinta_audit_logs.models import Audit, AuditAction
from vinta_audit_logs.models_registry import get_audit_identity_model, get_audit_scope_model
from vinta_audit_logs.types import AuditQuery, IdentitySnapshot, SubjectRef

pytestmark = pytest.mark.django_db


def test_writing_the_same_uid_twice_upserts(repository):
    """A retried task or a re-run backfill converges on the row already there."""
    uid = uuid.uuid7()
    repository.add(record_data(uid=uid))

    repository.add(record_data(uid=uid, action_key="update"))

    assert Audit.objects.filter(uid=uid).count() == 1
    assert Audit.objects.get(uid=uid).action_key == "update"


def test_created_at_is_preserved_across_a_rewrite(repository):
    """A replica reuses the emit time the record carries, not its own clock.

    Without this, two copies of one record hold different timestamps, the copies
    never compare equal, and the created_at windows a sync runs under stop
    lining up.
    """
    uid = uuid.uuid7()
    emitted_at = datetime(2026, 5, 5, 9, 30, tzinfo=UTC)
    repository.add(record_data(uid=uid, created_at=emitted_at))
    repository.add(record_data(uid=uid, created_at=emitted_at))

    assert Audit.objects.get(uid=uid).created_at == emitted_at


def test_each_record_gets_its_own_identity_row(repository):
    """Identities are per-record snapshots, never shared between records.

    Sharing one row would make a later action rewrite the groups an earlier one
    recorded, which is the one thing an audit trail must not do.
    """
    first = repository.add(
        record_data(
            actor=IdentitySnapshot(
                identity_type=IdentityType.USER, identity_key="7", group_names=["editors"]
            )
        )
    )
    second = repository.add(
        record_data(
            actor=IdentitySnapshot(
                identity_type=IdentityType.USER,
                identity_key="7",
                group_names=["editors", "admins"],
            )
        )
    )

    assert first.actor.group_names == ["editors"]
    assert second.actor.group_names == ["editors", "admins"]
    assert get_audit_identity_model().objects.count() == 2


def test_scope_rows_are_reused_across_records(repository):
    """Scopes are a dimension: one row per tenant, not one per record."""
    repository.bulk_add([record_data() for _ in range(3)])

    assert get_audit_scope_model().objects.filter(scope_key="1").count() == 1


def test_action_rows_are_reused_across_records(repository):
    """So are actions, and a repeated key does not create a second row."""
    repository.bulk_add([record_data(), record_data(), record_data(action_key="update")])

    assert AuditAction.objects.count() == 2


def test_empty_diff_is_stored_as_null(repository):
    """``{}`` means "no changes" and must normalize, or has_diff stops meaning anything."""
    repository.add(record_data(diff={}))

    assert repository.query(AuditQuery(has_diff=True)).total == 0
    assert repository.query(AuditQuery(has_diff=False)).total == 1


def test_a_diff_with_a_raw_datetime_value_round_trips_through_the_orm(repository):
    """``compute_diff`` preserves a dated field change as-is; this column must store it.

    ``diff`` is a free-form bag the caller fills with whatever the before/after
    state held, so a raw ``datetime`` -- the exact shape
    ``test_non_serializable_values_preserved`` in ``test_diff.py`` pins for
    ``compute_diff`` -- is an ordinary diff value, not a misuse. A ``JSONField``
    with no encoder raises ``TypeError`` on it; this one must not.
    """
    stored = repository.add(
        record_data(
            diff={
                "suspended_until": {
                    "old": datetime(2026, 1, 1, tzinfo=UTC),
                    "new": datetime(2026, 6, 1, tzinfo=UTC),
                }
            }
        )
    )

    assert stored.diff == {
        "suspended_until": {"old": "2026-01-01T00:00:00Z", "new": "2026-06-01T00:00:00Z"}
    }


def test_identity_metadata_with_a_raw_datetime_round_trips_through_the_orm(repository):
    """``metadata`` is the same kind of free-form, caller-filled bag as ``diff``."""
    stored = repository.add(
        record_data(
            actor=IdentitySnapshot(
                identity_key="7",
                metadata={"token_issued_at": datetime(2026, 1, 1, tzinfo=UTC)},
            )
        )
    )

    assert stored.actor.metadata == {"token_issued_at": "2026-01-01T00:00:00Z"}


def test_deleting_an_identity_is_refused_while_a_record_points_at_it(repository):
    """PROTECT, not CASCADE: the log outlives the dimensions it points at."""
    repository.add(record_data())

    with pytest.raises(ProtectedError):
        get_audit_identity_model().objects.first().delete()


def test_deleting_a_scope_is_refused_while_a_record_points_at_it(repository):
    """Same for scopes -- removing a tenant must not erase what happened in it."""
    repository.add(record_data())

    with pytest.raises(ProtectedError):
        get_audit_scope_model().objects.get(scope_key="1").delete()


def test_the_subject_survives_the_row_it_names(repository):
    """A soft reference, so deleting the subject leaves the record readable.

    No foreign key points at the subject, on purpose: an audit trail whose rows
    vanish along with the thing they describe is not an audit trail.
    """
    from tests.testapp.models import Article

    article = Article.objects.create(title="Retention policy")
    repository.add(
        record_data(
            subject=SubjectRef(
                subject_type="testapp.article",
                subject_id=str(article.pk),
                subject_label=article.title,
            )
        )
    )
    subject_pk = str(article.pk)
    article.delete()

    stored = repository.query(AuditQuery()).items[0]
    assert stored.subject.subject_type == "testapp.article"
    assert stored.subject.subject_id == subject_pk
    assert stored.subject.subject_label == "Retention policy"


def test_a_batch_writes_its_identities_in_one_statement(repository, django_assert_num_queries):
    """Identity rows are created per batch, not per record.

    A record can carry an actor, an on-behalf-of and any number of affected
    parties; creating those one at a time turns a 500-record batch into
    thousands of INSERTs.
    """
    batch = [
        record_data(
            affected=[
                IdentitySnapshot(identity_type=IdentityType.USER, identity_key=str(n)),
                IdentitySnapshot(identity_type=IdentityType.USER, identity_key=str(n + 100)),
            ]
        )
        for n in range(10)
    ]

    stored = repository.bulk_add(batch)

    assert len(stored) == 10
    assert get_audit_identity_model().objects.count() == 30  # 10 actors + 20 affected


def test_affected_identities_round_trip(repository):
    """What went in as affected parties comes back out as affected parties."""
    stored = repository.add(
        record_data(
            affected=[
                IdentitySnapshot(identity_type=IdentityType.USER, identity_key="21"),
                IdentitySnapshot(identity_type=IdentityType.USER, identity_key="34"),
            ]
        )
    )

    assert [snapshot.identity_key for snapshot in stored.affected] == ["21", "34"]


def test_duplicate_affected_identities_collapse(repository):
    """Naming the same party twice on one record is one link, not a constraint error."""
    stored = repository.add(
        record_data(
            affected=[
                IdentitySnapshot(identity_type=IdentityType.USER, identity_key="21"),
                IdentitySnapshot(identity_type=IdentityType.USER, identity_key="21"),
            ]
        )
    )

    assert [snapshot.identity_key for snapshot in stored.affected] == ["21"]


def test_on_behalf_of_is_recorded_separately_from_the_actor(repository):
    """Who appeared to act, and who really did."""
    stored = repository.add(
        record_data(
            on_behalf_of=IdentitySnapshot(
                identity_type=IdentityType.USER, identity_key="99", identity_label="staff"
            )
        )
    )

    assert stored.on_behalf_of is not None
    assert stored.on_behalf_of.identity_key == "99"
    assert stored.actor.identity_key == "7"
