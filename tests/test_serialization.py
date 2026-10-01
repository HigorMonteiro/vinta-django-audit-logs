"""The payload ``AuditService.record`` builds must actually survive ``json.dumps``.

``compute_diff`` is documented and tested (``test_non_serializable_values_preserved``
in ``test_diff.py``) to pass a diff's values through untouched, so a datetime, a
Decimal or a UUID is an ordinary, expected diff value -- not a misuse. These tests
pin the promise that such a value can cross the serialization boundary every
dispatcher and the ORM repository sit behind.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from vinta_audit_logs.types import AuditRecordData, IdentitySnapshot, SubjectRef

from .conftest import record_data


class TestSerializeRecordData:
    """Tests for serialize_record_data."""

    def test_plain_payload_round_trips(self):
        """A payload with no exotic values survives untouched."""
        from vinta_audit_logs.serialization import deserialize_record_data, serialize_record_data

        data = record_data(diff={"title": {"old": "a", "new": "b"}})
        payload = serialize_record_data(data)

        json.dumps(payload)  # must not raise
        rebuilt = deserialize_record_data(payload)
        assert rebuilt.diff == {"title": {"old": "a", "new": "b"}}

    def test_a_datetime_in_the_diff_survives_json_dumps(self):
        """A dated field change -- the exact shape compute_diff is tested to preserve."""
        from vinta_audit_logs.serialization import serialize_record_data

        data = record_data(
            diff={
                "suspended_until": {
                    "old": datetime(2026, 1, 1, tzinfo=UTC),
                    "new": datetime(2026, 6, 1, tzinfo=UTC),
                }
            }
        )
        payload = serialize_record_data(data)

        encoded = json.dumps(payload)  # must not raise TypeError
        assert "2026-01-01" in encoded
        assert "2026-06-01" in encoded

    def test_a_decimal_and_a_uuid_in_the_diff_survive_json_dumps(self):
        """Other common non-JSON-native values a project might diff."""
        from vinta_audit_logs.serialization import serialize_record_data

        token = uuid.uuid4()
        data = record_data(
            diff={
                "balance": {"old": Decimal("10.00"), "new": Decimal("12.50")},
                "api_token": {"old": None, "new": token},
            }
        )
        payload = serialize_record_data(data)

        json.dumps(payload)  # must not raise TypeError

    def test_metadata_with_a_datetime_survives_json_dumps(self):
        """``metadata`` is the same kind of free-form, caller-filled bag as diff."""
        from vinta_audit_logs.serialization import serialize_record_data

        data = record_data(
            actor=IdentitySnapshot(
                identity_key="7", metadata={"token_issued_at": datetime(2026, 1, 1, tzinfo=UTC)}
            )
        )
        payload = serialize_record_data(data)

        json.dumps(payload)  # must not raise TypeError

    def test_uid_and_created_at_keep_their_documented_string_shapes(self):
        """The hand-converted fields are untouched by the generic JSON pass."""
        from vinta_audit_logs.serialization import serialize_record_data

        uid = uuid.uuid7()
        created_at = datetime(2026, 5, 5, 9, 30, tzinfo=UTC)
        data = AuditRecordData(
            action_key="create",
            actor=IdentitySnapshot(identity_key="7"),
            subject=SubjectRef(subject_type="testapp.article", subject_id="1"),
            uid=uid,
            created_at=created_at,
        )
        payload = serialize_record_data(data)

        assert payload["uid"] == str(uid)
        assert payload["created_at"] == created_at.isoformat()

    def test_created_at_none_stays_none(self):
        """A record without an emit time still serializes to a JSON null."""
        from vinta_audit_logs.serialization import serialize_record_data

        data = record_data(created_at=None)
        payload = serialize_record_data(data)

        assert payload["created_at"] is None
