"""Turning a record into a JSON-safe dict and back.

Separate from ``tasks`` on purpose. A record has to survive a round trip through
JSON to cross *any* boundary -- a Celery broker, an HTTP call, a file on disk --
and only one of those needs Celery. Keeping the conversion here means a project
dispatching records without Celery never imports it.

The two functions are inverses and must change together.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime
from typing import Any

from vinta_audit_logs.types import (
    AuditRecordData,
    IdentitySnapshot,
    ScopeRef,
    SubjectRef,
)


def serialize_record_data(data: AuditRecordData) -> dict:
    """Reduce an ``AuditRecordData`` to a dict that survives ``json.dumps``.

    Only two fields are not JSON scalars, so only two are converted by hand:
    ``uid`` to its string form and ``created_at`` to ISO 8601.

    Args:
        data: The record data to serialize.

    Returns:
        A JSON-safe dict.
    """
    payload = dataclasses.asdict(data)
    payload["uid"] = str(data.uid)
    payload["created_at"] = data.created_at.isoformat() if data.created_at else None
    return payload


def _snapshot_from_payload(payload: dict[str, Any] | None) -> IdentitySnapshot | None:
    """Rebuild one ``IdentitySnapshot`` from its dict form, or None."""
    if payload is None:
        return None
    return IdentitySnapshot(
        identity_type=payload.get("identity_type", ""),
        identity_key=payload.get("identity_key", ""),
        identity_label=payload.get("identity_label", ""),
        user_id=payload.get("user_id"),
        is_staff=bool(payload.get("is_staff", False)),
        is_superuser=bool(payload.get("is_superuser", False)),
        group_names=list(payload.get("group_names") or []),
        permission_keys=list(payload.get("permission_keys") or []),
        metadata=dict(payload.get("metadata") or {}),
    )


def deserialize_record_data(payload: dict) -> AuditRecordData:
    """Rebuild an ``AuditRecordData`` from the dict :func:`serialize_record_data` produced.

    A payload missing ``uid`` gets a fresh one and a missing ``created_at`` leaves
    the repository to stamp its own clock. That only matters for messages already
    in flight across a deploy that added those fields, and it is the one case
    where a retry could write a duplicate -- the record has no stable identity to
    upsert on.

    Args:
        payload: The JSON-safe dict.

    Returns:
        The reconstructed record data.

    Raises:
        KeyError, TypeError, ValueError: The payload is malformed. Callers log and
            swallow rather than crashing a worker.
    """
    scope_payload = payload.get("scope") or {}
    subject_payload = payload["subject"]
    raw_uid = payload.get("uid")
    raw_created_at = payload.get("created_at")

    actor = _snapshot_from_payload(payload["actor"])
    if actor is None:
        raise ValueError("Audit payload carries no actor.")

    return AuditRecordData(
        action_key=payload["action_key"],
        actor=actor,
        subject=SubjectRef(
            subject_type=subject_payload["subject_type"],
            subject_id=subject_payload["subject_id"],
            subject_label=subject_payload.get("subject_label") or "",
        ),
        scope=ScopeRef(
            scope_type=scope_payload.get("scope_type", ""),
            scope_key=scope_payload.get("scope_key", ""),
            label=scope_payload.get("label", ""),
        ),
        on_behalf_of=_snapshot_from_payload(payload.get("on_behalf_of")),
        affected=[
            snapshot
            for raw in (payload.get("affected") or [])
            if (snapshot := _snapshot_from_payload(raw)) is not None
        ],
        diff=payload.get("diff"),
        action_name=payload.get("action_name", ""),
        action_content_type_key=payload.get("action_content_type_key", ""),
        uid=uuid.UUID(raw_uid) if raw_uid else uuid.uuid7(),
        created_at=datetime.fromisoformat(raw_created_at) if raw_created_at else None,
    )
