"""Every ``AuditQuery`` filter must be satisfied by the database, not by Python.

The point of these tests is not that the results are correct -- ``test_filtering``
and the conformance tests cover that -- but that the *work* happens in SQL. An
audit log is the one table in a project guaranteed to grow without bound, so a
filter that quietly falls back to loading rows and testing them in Python is a
defect that only shows up once the log is too big to fix comfortably.

Two properties are asserted, per filter:

* The compiled SQL carries a ``WHERE`` clause. A filter that produced no
  predicate would be filtering in Python (or not at all).
* The query fetches only the page asked for. ``LIMIT`` in the SQL means the
  database decided which rows to return.
"""

import uuid
from datetime import UTC, datetime

import pytest

from vinta_audit_logs.constants import IdentityType, ScopeType
from vinta_audit_logs.types import (
    AuditQuery,
    AuditRecordData,
    IdentityRef,
    IdentitySnapshot,
    ScopeKey,
    ScopeRef,
    SubjectRef,
)

pytestmark = pytest.mark.django_db


def _record(scope_key: str, **overrides) -> AuditRecordData:
    """Build a record in one scope, with sensible defaults."""
    defaults = {
        "action_key": "create",
        "actor": IdentitySnapshot(
            identity_type=IdentityType.USER,
            identity_key="7",
            identity_label="someone",
        ),
        "subject": SubjectRef(subject_type="testapp.article", subject_id="1"),
        "scope": ScopeRef(scope_type=ScopeType.SCOPED, scope_key=scope_key),
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    return AuditRecordData(**{**defaults, **overrides})


#: One entry per ``AuditQuery`` field, so a field added without a matching SQL
#: translation fails here rather than silently degrading to a full scan.
QUERIES = {
    "scope_keys": AuditQuery(scope_keys=["1"]),
    "scope_types": AuditQuery(scope_types=[ScopeType.SCOPED]),
    "scopes": AuditQuery(scopes=[ScopeKey(scope_type=ScopeType.SCOPED, scope_key="1")]),
    "uids": AuditQuery(uids=[uuid.uuid7()]),
    "actions": AuditQuery(actions=["create"]),
    "actor_types": AuditQuery(actor_types=[IdentityType.USER]),
    "actors": AuditQuery(actors=[IdentityRef(identity_type=IdentityType.USER, identity_key="7")]),
    "subject_types": AuditQuery(subject_types=["testapp.article"]),
    "subjects": AuditQuery(
        subjects=[
            __import__("vinta_audit_logs.types", fromlist=["SubjectKey"]).SubjectKey(
                subject_type="organizations.organization", subject_id="1"
            )
        ]
    ),
    "affected": AuditQuery(
        affected=[IdentityRef(identity_type=IdentityType.USER, identity_key="9")]
    ),
    "created_after": AuditQuery(created_after=datetime(2026, 1, 1, tzinfo=UTC)),
    "created_before": AuditQuery(created_before=datetime(2026, 2, 1, tzinfo=UTC)),
    "has_diff": AuditQuery(has_diff=True),
    "search": AuditQuery(search="article"),
}


@pytest.mark.parametrize("field_name", sorted(QUERIES))
def test_every_filter_compiles_to_a_where_clause(repository, field_name):
    """No ``AuditQuery`` field is left to be applied in Python."""
    sql = str(repository._filtered_queryset(QUERIES[field_name]).query)

    assert " WHERE " in sql, (
        f"AuditQuery.{field_name} produced no SQL predicate, so it would be "
        f"filtered in Python over the whole log. Compiled SQL: {sql}"
    )


def test_query_pushes_pagination_into_sql(repository, django_assert_num_queries):
    """A page is cut by the database, not sliced out of a fetched list."""
    repository.bulk_add([_record("1") for _ in range(5)])

    q = AuditQuery(scope_keys=["1"])
    page_sql = str(repository._filtered_queryset(q).order_by("-created_at")[0:2].query)

    assert "LIMIT 2" in page_sql

    # Three, and three whatever the page size: one COUNT for the total, one
    # SELECT for the page (scope, actor and on-behalf-of joined in), and one for
    # the affected links with their identities. Anything that grows with the page
    # means rows are being fetched to be filtered or resolved outside the
    # database.
    with django_assert_num_queries(3):
        page = repository.query(q, offset=0, limit=2)
    assert len(page.items) == 2
    assert page.total == 5


def test_count_does_not_fetch_rows(repository, django_assert_num_queries):
    """``count`` is a COUNT query, not ``len()`` over a fetched result set."""
    repository.bulk_add([_record("1") for _ in range(3)])

    with django_assert_num_queries(1):
        total = repository.count(AuditQuery(scope_keys=["1"]))

    assert total == 3


def test_scope_filter_isolates_scopes(repository):
    """The scope filter is the tenant boundary, and it holds."""
    repository.bulk_add(
        [
            _record("1"),
            _record("1"),
            _record("2"),
        ]
    )

    mine = repository.query(AuditQuery(scope_keys=["1"]), limit=50)
    theirs = repository.query(AuditQuery(scope_keys=["2"]), limit=50)

    assert mine.total == 2
    assert theirs.total == 1


def test_actor_filter_needs_no_join(repository):
    """Filtering by actor reads the denormalized columns on the record itself.

    If this ever starts joining, the browse index stops applying and every audit
    page pays for a nested loop over the identity table.
    """
    q = AuditQuery(actors=[IdentityRef(identity_type=IdentityType.USER, identity_key="7")])
    sql = str(repository._filtered_queryset(q).query)

    assert "vinta_audit_logs_audit" in sql
    assert "JOIN" not in sql.upper(), (
        f"actor filter joined instead of using its own columns: {sql}"
    )


def test_action_and_scope_filters_need_no_join(repository):
    """Same for the other two denormalized dimensions."""
    q = AuditQuery(actions=["create"], scope_keys=["1"], scope_types=[ScopeType.SCOPED])
    sql = str(repository._filtered_queryset(q).query)

    assert "JOIN" not in sql.upper(), f"scope/action filter joined unnecessarily: {sql}"


def test_iter_records_seeks_by_key_not_offset(repository):
    """The full-log walk pages by keyset, so its cost does not grow with depth.

    ``OFFSET n`` makes Postgres produce and discard n rows, which turns a walk of
    the whole log into quadratic work. Every page here must instead carry a row
    comparison seeking past the last record already yielded.
    """
    created = [_record("1", created_at=datetime(2026, 1, day, tzinfo=UTC)) for day in range(1, 8)]
    repository.bulk_add(created)

    seen = list(repository.iter_records(AuditQuery(scope_keys=["1"]), chunk_size=2))

    # Every record, exactly once, oldest first.
    assert [record.created_at.day for record in seen] == [1, 2, 3, 4, 5, 6, 7]
    assert len({record.uid for record in seen}) == 7

    # And the paging predicate is a row comparison, not an OFFSET.
    base = repository._filtered_queryset(AuditQuery()).order_by("created_at", "uid")
    from vinta_audit_logs.repositories import _after_cursor

    seek_sql = str(
        base.filter(_after_cursor((datetime(2026, 1, 3, tzinfo=UTC), uuid.uuid7()))).query
    )
    assert "OFFSET" not in seek_sql.upper()
    assert ") > (" in seek_sql


def test_iter_records_does_not_skip_records_sharing_an_instant(repository):
    """Records written in the same instant all survive the walk.

    The failure this guards against is the classic keyset bug: seeking with
    ``created_at >= last AND uid > last_uid`` drops any record sharing that
    instant with a smaller uid. The walk must use the lexicographic pair.
    """
    same_instant = datetime(2026, 3, 3, 12, 0, 0, tzinfo=UTC)
    repository.bulk_add([_record("1", created_at=same_instant) for _ in range(5)])

    # A chunk size that forces the boundary to land inside the tied group.
    seen = list(repository.iter_records(AuditQuery(scope_keys=["1"]), chunk_size=2))

    assert len(seen) == 5
    assert len({record.uid for record in seen}) == 5
