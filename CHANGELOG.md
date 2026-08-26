# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] - 2026-08-26

### Fixed

- `models_registry` imported `django.db.models` under `TYPE_CHECKING` while having
  no `from __future__ import annotations`, so on Python 3.14 the module raised
  `NameError: name 'models' is not defined` the moment anything read its
  `__annotations__`. Django's introspection and most dependency-injection wiring
  do exactly that, so a host project failed at `manage.py check` — importing the
  module, calling its functions and type-checking it all passed. A test now walks
  every module in the package and reads its annotations the way a host would.

## [0.1.0] - 2026-08-26

First release. Extracted from a production Django project, generalised so the two
things that genuinely differ between projects — what a tenant is and what an actor
is — are swappable models rather than assumptions.

### Added

- `Audit`: an append-only record of one action, by one actor, on one subject.
  Written once and never updated, which is what lets it denormalize the columns
  its indexes and any future partition key need.
- `uid`, a UUIDv7 generated at emit time and carried into every repository a record
  is written to. Every write is an upsert on it, so a retried task, a re-run
  backfill and a catching-up replica all converge on one row instead of appending
  copies. Version 7 rather than 4 so values arrive in roughly generation order and
  the unique index takes its inserts at its right edge.
- `AuditScope` and `AuditIdentity`, both **swappable** through `AUDIT_SCOPE_MODEL`
  and `AUDIT_IDENTITY_MODEL`. A project points them at models with real foreign
  keys and columns of its own; nothing in the log has to know.
- `AbstractAuditScope`, which owns the one rule every scope has to hold —
  `scope_type` and the scope value agree — checked against the final state on both
  insert and update, and backed by a CHECK constraint because `save()` is bypassed
  by `bulk_create`.
- `AuditAction`: a table rather than free strings, so a mistyped action key fails at
  write time instead of producing a record that silently matches no filter.
- Identity snapshots that record what an actor *could do* at the time — groups,
  permissions, staff flags — captured synchronously in the request, because
  re-reading them in the worker records the wrong answer. One row per record, never
  shared: sharing would let a later action rewrite what an earlier one observed.
- Actors that are not people. `user` is optional and `SET_NULL`, so a scheduled job,
  an API token or a deleted account are all representable and none of them can take
  the trail down with them.
- `on_behalf_of`, for when one principal acts as another, and `affected_identities`,
  for everyone an action touched as distinct from who took it.
- `AuditQuery`: one portable filter object, with uniform rules — fields AND, values
  OR, `None` inactive, and `[]` an active filter nothing satisfies.
- `DjangoORMAuditRepository`, translating every field of that query into SQL. No
  filter joins: each reads a denormalized column on the audit row, and the four
  browse indexes lead with `scope_key` and end with `created_at DESC` so the
  ordering comes from the index rather than a sort.
- `iter_records`, which walks a whole log by seeking on `(created_at, uid)` rather
  than paging with `OFFSET` — constant cost per chunk however deep the walk goes.
- `InMemoryAuditRepository` and `vinta_audit_logs.filtering`: a second
  implementation of the interface, and the pure-Python definition of what every
  filter means. Both are pinned against the SQL translation by a conformance suite.
- Multiple repositories per service: one main write plus best-effort replication to
  any number of additional backends, and `sync_repository` to reconcile whatever
  replication was allowed to lose. Safe to re-run and safe to overlap with live
  replication, because every write is an upsert.
- `AUDIT_RECORD_DISPATCHER`, with a Celery dispatcher and an inline one. Celery is
  an optional dependency; `vinta_audit_logs.tasks` is the only module importing it.
  Whichever dispatcher runs, it runs inside `transaction.on_commit`.
- A read-only admin sourced from the repository rather than the ORM, so it renders a
  log held anywhere: filtered changelist, detail view and a streaming CSV export.
- `AuditQuery.active_extension_fields()`, so a project that extends the query object
  gets an error from a backend that cannot apply the new fields rather than results
  that look filtered and are not.
- `compute_diff`, and the invariant it upholds: a diff is None or a non-empty dict,
  never `{}`, so `has_diff` keeps meaning something.

[Unreleased]: https://github.com/vintasoftware/vinta-django-audit-logs/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/vintasoftware/vinta-django-audit-logs/releases/tag/v0.1.1
[0.1.0]: https://github.com/vintasoftware/vinta-django-audit-logs/releases/tag/v0.1.0
