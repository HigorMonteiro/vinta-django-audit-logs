"""The two repositories must answer the same question the same way.

The ORM repository pushes filters into SQL; the in-memory one runs them through
``vinta_audit_logs.filtering``. Those are two independent implementations of one
contract, and this file is what stops them drifting: the same records go into
both, the same ``AuditQuery`` is asked of both, and the answers must match.

A disagreement here means one of two things, both worth catching: the SQL
translation is wrong, or the Python reference is. Neither is discoverable by
testing either backend alone.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from vinta_audit_logs.constants import IdentityType, ScopeType
from vinta_audit_logs.types import (
    AuditQuery,
    AuditRecordData,
    IdentityRef,
    IdentitySnapshot,
    ScopeKey,
    ScopeRef,
    SubjectKey,
    SubjectRef,
)

pytestmark = pytest.mark.django_db

BASE_TIME = datetime(2026, 4, 1, tzinfo=UTC)

#: Two scopes, as bare keys -- this package has no tenant model of its own.
SCOPE = "1"
OTHER_SCOPE = "2"


def _population(scope_key: str, other_scope_key: str) -> list[AuditRecordData]:
    """A spread of records exercising every filter dimension at once."""
    return [
        AuditRecordData(
            action_key="create",
            actor=IdentitySnapshot(
                identity_type=IdentityType.USER, identity_key="1", identity_label="alice"
            ),
            subject=SubjectRef(subject_type="testapp.article", subject_id="10"),
            scope=ScopeRef(scope_type=ScopeType.SCOPED, scope_key=scope_key),
            created_at=BASE_TIME,
            uid=uuid.uuid7(),
        ),
        AuditRecordData(
            action_key="update",
            actor=IdentitySnapshot(
                identity_type=IdentityType.USER, identity_key="2", identity_label="bob"
            ),
            subject=SubjectRef(subject_type="testapp.article", subject_id="10"),
            scope=ScopeRef(scope_type=ScopeType.SCOPED, scope_key=scope_key),
            diff={"name": {"old": "a", "new": "b"}},
            created_at=BASE_TIME + timedelta(days=1),
            affected=[IdentitySnapshot(identity_type=IdentityType.USER, identity_key="99")],
            uid=uuid.uuid7(),
        ),
        AuditRecordData(
            action_key="delete",
            actor=IdentitySnapshot(identity_type=IdentityType.SYSTEM, identity_key=""),
            subject=SubjectRef(subject_type="testapp.note", subject_id="55"),
            scope=ScopeRef(scope_type=ScopeType.SCOPED, scope_key=other_scope_key),
            created_at=BASE_TIME + timedelta(days=2),
            uid=uuid.uuid7(),
        ),
        AuditRecordData(
            action_key="create",
            actor=IdentitySnapshot(identity_type=IdentityType.SERVICE, identity_key="job-7"),
            subject=SubjectRef(subject_type="testapp.note", subject_id="56"),
            scope=ScopeRef.global_scope(),
            created_at=BASE_TIME + timedelta(days=3),
            uid=uuid.uuid7(),
        ),
    ]


def _queries(scope_key: str, other_scope_key: str) -> dict[str, AuditQuery]:
    return {
        "everything": AuditQuery(),
        "one scope": AuditQuery(scope_keys=[scope_key]),
        "two scopes": AuditQuery(scope_keys=[scope_key, other_scope_key]),
        "global only": AuditQuery(scope_types=[ScopeType.GLOBAL]),
        "scope pair": AuditQuery(
            scopes=[ScopeKey(scope_type=ScopeType.SCOPED, scope_key=scope_key)]
        ),
        "one action": AuditQuery(actions=["create"]),
        "two actions": AuditQuery(actions=["create", "delete"]),
        "no actions": AuditQuery(actions=[]),
        "actor type": AuditQuery(actor_types=[IdentityType.USER]),
        "actor pair": AuditQuery(
            actors=[IdentityRef(identity_type=IdentityType.USER, identity_key="2")]
        ),
        "system actor": AuditQuery(
            actors=[IdentityRef(identity_type=IdentityType.SYSTEM, identity_key="")]
        ),
        "subject type": AuditQuery(subject_types=["testapp.note"]),
        "subject pair": AuditQuery(
            subjects=[SubjectKey(subject_type="testapp.article", subject_id="10")]
        ),
        "affected": AuditQuery(
            affected=[IdentityRef(identity_type=IdentityType.USER, identity_key="99")]
        ),
        "no affected": AuditQuery(affected=[]),
        "has diff": AuditQuery(has_diff=True),
        "no diff": AuditQuery(has_diff=False),
        "window": AuditQuery(
            created_after=BASE_TIME + timedelta(days=1),
            created_before=BASE_TIME + timedelta(days=3),
        ),
        "search subject": AuditQuery(search="note"),
        "search actor": AuditQuery(search="alice"),
        "combined": AuditQuery(
            scope_keys=[scope_key],
            actions=["update"],
            has_diff=True,
        ),
    }


@pytest.fixture
def both(repository, memory_repository):
    """Both repositories, holding the identical set of records."""
    records = _population(SCOPE, OTHER_SCOPE)
    repository.bulk_add(records)
    memory_repository.bulk_add(records)
    return repository, memory_repository


@pytest.mark.parametrize("label", sorted(_queries(SCOPE, OTHER_SCOPE)))
def test_both_repositories_match_the_same_records(both, label):
    """Same filter, same records -- whichever backend is asked."""
    orm, memory = both
    q = _queries(SCOPE, OTHER_SCOPE)[label]

    orm_page = orm.query(q, offset=0, limit=50)
    memory_page = memory.query(q, offset=0, limit=50)

    assert orm_page.total == memory_page.total, f"{label}: totals disagree"
    assert [record.uid for record in orm_page.items] == [
        record.uid for record in memory_page.items
    ], f"{label}: matched records disagree"


def test_an_empty_list_filter_matches_nothing_in_both(both):
    """``[]`` is an active filter nothing satisfies -- SQL's ``IN ()``.

    The distinction from ``None`` is the one that bites: code building a filter
    from a computed set must get an empty result, not the whole log.
    """
    orm, memory = both

    assert orm.query(AuditQuery(actions=[])).total == 0
    assert memory.query(AuditQuery(actions=[])).total == 0
    assert orm.query(AuditQuery(actions=None)).total == 4
    assert memory.query(AuditQuery(actions=None)).total == 4


def test_both_iterate_in_the_same_stable_order(both):
    """A sync walks both backends the same way, or the windows stop lining up."""
    orm, memory = both

    orm_walk = [record.uid for record in orm.iter_records(AuditQuery(), chunk_size=2)]
    memory_walk = [record.uid for record in memory.iter_records(AuditQuery(), chunk_size=2)]

    assert orm_walk == memory_walk
    assert len(orm_walk) == 4


def test_a_record_survives_a_round_trip_through_both(both):
    """The portable DTO is genuinely portable: out of one, into the other, equal."""
    orm, memory = both
    original = next(iter(orm.iter_records(AuditQuery(has_diff=True))))

    memory.bulk_add([original.to_data()])
    copy = memory.get_by_uid(original.uid)

    assert copy is not None
    assert copy.uid == original.uid
    assert copy.created_at == original.created_at
    assert copy.action_key == original.action_key
    assert copy.scope == original.scope
    assert copy.diff == original.diff
    assert [s.identity_key for s in copy.affected] == [s.identity_key for s in original.affected]
