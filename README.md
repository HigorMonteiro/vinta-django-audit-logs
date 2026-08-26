# vinta-django-audit-logs

An append-only audit log for Django. Records are written once and never updated,
scoped to whatever a tenant means in your project, and read back through one
filter object that means the same thing whichever backend holds them.

```python
audit_service.record(
    action="membership.role_changed",
    actor=audit_service.identity_from_user(request.user),
    subject=audit_service.subject_from_instance(membership),
    scope=ScopeRef(scope_type=ScopeType.SCOPED, scope_key=str(organization.pk)),
    diff={"role": {"old": "member", "new": "admin"}},
)
```

## Why

Most Django audit packages capture rows: a signal or a trigger fires on save and
you get a serialized before-and-after. That is a good answer to "what changed in
this table" and a poor one to the questions people actually bring to an audit
log — *who did this, on whose behalf, under what permissions, and to whom*.

This app answers those by recording **semantic events** rather than row diffs,
and by taking three design positions the shape of the schema follows from:

- **The log outlives everything it describes.** Deleting a user, a tenant or a
  model does not delete or corrupt the records about them. Every reference to
  something your project owns is soft — a type string and a key — with the
  foreign keys reserved for rows this app owns and never deletes.
- **What the actor could do is part of the record.** Groups, permissions and
  whatever else you capture are snapshotted at emit time, in the request. An
  audit trail that re-reads the actor's permissions when the worker runs records
  the wrong permissions.
- **A record has the same identity everywhere.** Every record carries a UUIDv7
  `uid` generated once, and every write is an upsert on it. Retry the task,
  re-run the backfill, replicate to a warehouse — you converge on one row rather
  than accumulating copies.

## Install

```bash
uv add vinta-django-audit-logs
```

```python
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "vinta_audit_logs",
    ...,
]

AUDIT_SERVICE_FACTORY = "myproject.audit.build_audit_service"
AUDIT_REPOSITORY_FACTORY = "myproject.audit.build_audit_repository"
```

```python
# myproject/audit.py
from vinta_audit_logs.repositories import DjangoORMAuditRepository
from vinta_audit_logs.services import AuditService


def build_audit_repository():
    return DjangoORMAuditRepository()


def build_audit_service():
    return AuditService(repository=build_audit_repository())
```

```bash
python manage.py migrate
```

That is enough to run. The two model settings default to the models this app
ships, and records dispatch to Celery — see [Dispatching](#dispatching) if you
use a different queue or none.

## The shape of a record

Four tables. The split between them is the whole design.

`Audit` is the log: append-only, written once, never updated. Because a row is
immutable, it can denormalize freely — a copied value on a row that never
changes has no opportunity to drift.

`AuditScope`, `AuditIdentity` and `AuditAction` are dimensions the log points at.
All three are owned by this app and none are ever deleted, which is what makes
real foreign keys safe: the key refuses a bad id at write time, and `PROTECT`
means no cascade can reach the log. Rows *your project* owns — a user, a content
type — are held at arm's length instead, behind a nullable reference plus a
snapshot that stays readable after the row is gone.

| On the record | Why it is there |
| --- | --- |
| `uid` | UUIDv7, the record's identity in every repository. Time-ordered, so the unique index takes its inserts at its right edge instead of scattered across the tree. |
| `created_at` | When the action *happened*, stamped in the request — not when a worker got round to the write. |
| `action` + `action_key` | The FK refuses a mistyped action at write time; the copy means reads never join. |
| `scope` + `scope_type` + `scope_key` | Same pairing. `scope_key` is what the browse indexes lead with and what a future partition key would be. |
| `actor` + `actor_type` + `actor_key`, `on_behalf_of` | Who acted, and who they acted for. |
| `subject_content_type_key` + `subject_pk` + `subject_label` | A soft reference. Survives the row it names. |
| `diff` | `{field: {"old": ..., "new": ...}}`, or NULL. Never `{}`. |
| `affected_identities` | Everyone the action touched, as distinct from who took it. |

## Scopes

A scope is what a record belongs to: a tenant, a workspace, an account, or the
installation at large. `ScopeType.GLOBAL` covers actions with no tenant behind
them; `SCOPED` is everything else.

`scope_key` is the portable spelling — a string every backend can index,
partition on, and carry to another repository unchanged. It must be **stable for
the life of the scope**: records are found by it, so a key that changes orphans
everything already written under the old one. A primary key is a good key; a
renameable slug is not.

### Pointing the scope at your own model

```python
# settings.py — a top-level setting, because Meta.swappable resolves against one
AUDIT_SCOPE_MODEL = "myproject.OrganizationAuditScope"
```

```python
from django.db import models
from vinta_audit_logs.constants import ScopeType
from vinta_audit_logs.models import AbstractAuditScope


class OrganizationAuditScope(AbstractAuditScope[Organization]):
    organization = models.ForeignKey(Organization, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        swappable = "AUDIT_SCOPE_MODEL"

    @property
    def scope(self):
        return self.organization

    @scope.setter
    def scope(self, value):
        self.organization = value
        self.scope_type = ScopeType.GLOBAL if value is None else ScopeType.SCOPED

    @scope.deleter
    def scope(self):
        self.organization = None
        self.scope_type = ScopeType.GLOBAL

    def build_scope_key(self) -> str:
        return "" if self.organization_id is None else str(self.organization_id)
```

`PROTECT`, not `CASCADE`: deleting a tenant must not delete the record of what
happened inside it.

## Identities

**One identity row per audit record**, not one per actor. The columns are a
snapshot — the groups, permissions and display name the actor carried *when they
acted*, which is the question an audit trail is asked. Deduplicating per user
would answer a different one.

Not every actor is a person. A scheduled job, an API token and an internal
service all take auditable actions, so `user` is optional and the identifying
columns — `identity_type`, `identity_key`, `identity_label` — are always
populated whether or not a row in your user table backs them. `user` is
`SET_NULL`, so an erasure request neither fails nor takes the trail with it.

`IdentityType` ships `user`, `system` and `service`, and is deliberately **not**
enforced as `choices`: your project has actor kinds of its own, and adding one
should not need a migration.

Swap the model the same way as the scope, via `AUDIT_IDENTITY_MODEL`.

## Recording

Build the snapshot **synchronously**, in the request. Everything it captures is
mutable state that may have changed — or been deleted — by the time the write
runs.

```python
audit_service.record(
    action="calendar.event.reschedule",
    actor=audit_service.identity_from_user(request.user),
    subject=audit_service.subject_from_instance(event, label=event.title),
    scope=my_scope_ref(organization),
    on_behalf_of=audit_service.identity_from_user(impersonated_by),  # optional
    affected=[...],  # optional
    diff=compute_diff(before, after),  # optional
)
```

`subject_label` is left empty unless you pass one. Deliberately not defaulted to
`str(instance)`: a model `__str__` can dereference a related row and raise, and
building the audit payload must never break the business action it describes.

### Extending the builders

`AuditService` knows how to write a record. It does not know what an actor is in
your project. Subclass it:

```python
class OrganizationAuditService(AuditService):
    def actor_from_membership(self, membership):
        return IdentitySnapshot(
            identity_type="membership",
            identity_key=str(membership.user_id),
            user_id=membership.user_id,
            group_names=sorted(membership.groups.values_list("name", flat=True)),
            metadata={"membership_group_ids": [...]},
        )
```

The write-side half of the same seam lives on the repository — see below. They
are separate objects because one runs in the request and one runs in the worker.

## Dispatching

`AUDIT_RECORD_DISPATCHER` names what happens to a record after the transaction
commits. Two ship:

| Dispatcher | When |
| --- | --- |
| `vinta_audit_logs.dispatch.dispatch_via_celery` (default) | You have Celery. Set `AUDIT_CELERY_APP` to your app's dotted path. |
| `vinta_audit_logs.dispatch.dispatch_inline` | No queue. The write happens in the request. |

Whichever runs, it runs inside `transaction.on_commit` — an audit record for an
action that rolled back is a lie, and that is enforced once rather than per
dispatcher. A dispatcher that raises is logged and swallowed: instrumentation
must never break the action it describes.

Celery is an optional dependency (`vinta-django-audit-logs[celery]`), and
`vinta_audit_logs.tasks` is the only module that imports it.

## Reading

One filter object, and it means the same thing pointed at any backend:

```python
from vinta_audit_logs.types import AuditQuery

page = audit_service.query(
    AuditQuery(
        scope_keys=[str(organization.pk)],
        actions=["calendar.event.reschedule"],
        created_after=last_month,
    ),
    limit=50,
)
```

Every filter is a set membership test, so one field covers "this one" and "any of
these six". The rules are uniform: fields AND together, values within a field OR
together, `None` means inactive, and `[]` is an **active filter that nothing
satisfies** — the SQL `IN ()`, so a filter built from a computed set returns
nothing rather than silently returning everything.

`DjangoORMAuditRepository` translates all of it to SQL. Filtering, ordering and
pagination happen in the database; only one page of rows ever reaches Python.
Every filter reads a denormalized column on the audit row, so none of them join,
and the four browse indexes all lead with `scope_key` and end with
`created_at DESC` — the ordering comes from the index rather than a sort.

To walk the whole log, use `iter_records`. It seeks by key on `(created_at, uid)`
rather than paging with `OFFSET`, so the cost of each chunk is the same on the
ten-thousandth page as on the first.

### Extending the filters

`AuditQuery` cannot name a column that exists only because you swapped a model
in. Subclass it and teach one repository about the new fields:

```python
@dataclass(frozen=True)
class OrganizationAuditQuery(AuditQuery):
    actor_user_emails: list[str] | None = None


class OrganizationAuditRepository(DjangoORMAuditRepository):
    def _filtered_queryset(self, q):
        qs = super()._filtered_queryset(q)
        if isinstance(q, OrganizationAuditQuery) and q.actor_user_emails is not None:
            qs = qs.filter(actor__user__email__in=q.actor_user_emails)
        return qs
```

These **join**, which is the point of them and also their cost: they do not use
the browse indexes and they get slower as the log grows. Use them for
investigation, and pair them with a portable filter — `scope_keys` plus an email
lets Postgres cut to one tenant on the index before joining.

A backend that cannot apply a field must refuse rather than ignore it, or you get
results that look filtered and are not. `AuditQuery.active_extension_fields()`
reports which extension fields are set, and `record_matches` raises
`NotImplementedError` rather than guessing.

## Storage backends

`AuditRepository` is read-and-append, with append defined as an upsert on `uid`.
Three things come with implementing it:

- `DjangoORMAuditRepository` — the ORM, and the seam where portable DTOs meet a
  concrete schema. Override `build_scope_defaults`, `build_identity_defaults` and
  `attach_identity_relations` for the columns your swapped models add.
- `InMemoryAuditRepository` — the second implementation, kept honest by running
  its reads through `vinta_audit_logs.filtering`.
- Your own. A backend that cannot push filters down gets a correct `query` for
  free by handing its records to `filtering.apply_query`.

A service holds one **main** repository and any number of additional ones:

```python
AuditService(repository=orm_repo, additional_repositories={"warehouse": loader})
```

Every record is written to the main repository first — that write is what the
trail's durability rests on — and then *tentatively* replicated to each
additional one. A replica failing is logged and swallowed, never allowed to
unwind the main write. Reconciling what a replica missed is `sync_repository`'s
job, and because every write is an upsert on `uid`, re-running a sync over a
window that is already in step rewrites those records with identical content
rather than duplicating them.

## Admin

The app registers a read-only changelist for `Audit`, sourced from
`AuditRepository` rather than the ORM — so the same admin renders a log held
anywhere. Filters, a detail view and a streaming CSV export. Add, change and
delete are all denied: the log is append-only, written through the service.

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| `AUDIT_SCOPE_MODEL` | `"vinta_audit_logs.AuditScope"` | The scope model. |
| `AUDIT_IDENTITY_MODEL` | `"vinta_audit_logs.AuditIdentity"` | The identity model. |
| `AUDIT_RECORD_DISPATCHER` | `dispatch_via_celery` | What happens after commit. |
| `AUDIT_CELERY_APP` | — | Your Celery app, for the Celery dispatcher. |
| `AUDIT_SERVICE_FACTORY` | — | Zero-arg callable returning an `AuditService`. |
| `AUDIT_REPOSITORY_FACTORY` | — | Zero-arg callable returning an `AuditRepository`. |

## Development

```bash
uv sync --all-groups
uv run pytest
uv run pytest tests/swapped --ds=tests.settings_swapped   # the swapped-model run
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run tox                                                # the whole matrix
```

Both models are swappable, and `Meta.swappable` resolves once per process, so the
swapped configuration is its own settings module and its own pytest run rather
than an `override_settings`.

## License

MIT. See [LICENSE](LICENSE).
