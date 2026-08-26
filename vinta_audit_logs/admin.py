"""Audit administration -- repository-backed, read-only changelist and detail view.

Architecture
------------
``AuditAdmin`` is registered for the ``Audit`` model so the changelist appears in
the admin index with the usual auth, permission and breadcrumb handling. The
``ModelAdmin`` is a *shell*:

- add / change / delete permissions always return ``False`` -- the log is
  append-only, written through ``AuditService``.
- ``changelist_view`` parses filters from the query string, builds an
  ``AuditQuery``, calls ``AuditRepository.query`` and renders a custom template.
  Django's ORM ChangeList machinery is bypassed entirely, so the admin works
  against ANY repository backend, not just the ORM one.
- ``detail_view`` and ``export_view`` are registered through ``get_urls()`` and
  read through the same repository.

The repository comes from :func:`get_audit_repository`, which resolves the
``AUDIT_REPOSITORY_FACTORY`` setting. A dotted path rather than a DI container:
this app is meant to be installed, and a project should not have to adopt a
particular DI library to see its own audit log.
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import admin
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.http.response import Http404, HttpResponseBase, StreamingHttpResponse
from django.template.response import TemplateResponse
from django.urls import path
from django.utils.module_loading import import_string

from vinta_audit_logs.constants import ScopeType
from vinta_audit_logs.models import Audit
from vinta_audit_logs.types import AuditQuery

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable

    from vinta_audit_logs.repositories import AuditRepository


logger = logging.getLogger(__name__)

_DEFAULT_PER_PAGE = 50
_MAX_PER_PAGE = 200


def get_audit_repository() -> AuditRepository | None:
    """Resolve the repository the admin reads through.

    ``AUDIT_REPOSITORY_FACTORY`` names a zero-argument callable returning an
    ``AuditRepository``. Returns None -- rather than raising -- when it is unset
    or broken, so a misconfiguration renders an empty changelist with a logged
    error instead of a 500 on the admin index.
    """
    path_setting = getattr(settings, "AUDIT_REPOSITORY_FACTORY", None)
    if not path_setting:
        logger.error(
            "AUDIT_REPOSITORY_FACTORY is not set; the audit admin has no repository to read from."
        )
        return None
    try:
        return import_string(path_setting)()
    except Exception:
        logger.exception(
            "AUDIT_REPOSITORY_FACTORY %r could not produce an AuditRepository.",
            path_setting,
        )
        return None


def _parse_int(value: str | None) -> int | None:
    """Return an int from a string, or None if blank/invalid."""
    if not value:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO 8601 datetime, returning None for blank or invalid input.

    A naive value is assumed to be UTC: the admin filter fields are date/time
    inputs with no timezone, and comparing a naive value against the aware
    ``created_at`` column would raise.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _parse_has_diff(value: str | None) -> bool | None:
    """Map the tri-state ``has_diff`` filter to True / False / None."""
    if value == "yes":
        return True
    if value == "no":
        return False
    return None


def _build_audit_query(params: dict[str, str | list[str]]) -> AuditQuery:
    """Build an ``AuditQuery`` from request GET params.

    ``params`` must be the result of ``dict(QueryDict_instance)``, which yields
    ``dict[str, list[str]]``. Do NOT pass ``QueryDict.dict()`` -- that yields
    scalar strings and bypasses the list-branch normalisation below. Each value
    is normalised to a single non-blank string, so an unset filter field is
    treated as "no filter" rather than as a filter on the empty string.
    """

    def _first(v: str | list[str] | None) -> str | None:
        if v is None:
            return None
        if isinstance(v, list):
            return (v[0] or None) if v else None
        return v or None

    action = _first(params.get("action"))
    actor_type = _first(params.get("actor_type"))
    scope_key = _first(params.get("scope_key"))
    scope_type = _first(params.get("scope_type"))

    # Every AuditQuery filter is a set membership test, so each single-valued
    # admin filter becomes a one-element list. `None` (rather than `[]`) when the
    # param is absent: an empty list is an active filter that matches nothing.
    return AuditQuery(
        scope_keys=[scope_key] if scope_key else None,
        scope_types=[scope_type] if scope_type else None,
        actions=[action] if action else None,
        actor_types=[actor_type] if actor_type else None,
        created_after=_parse_datetime(_first(params.get("created_after"))),
        created_before=_parse_datetime(_first(params.get("created_before"))),
        has_diff=_parse_has_diff(_first(params.get("has_diff"))),
        search=_first(params.get("search")),
    )


#: The GET params that survive into pagination and export links.
_FILTER_PARAMS = (
    "action",
    "actor_type",
    "scope_key",
    "scope_type",
    "created_after",
    "created_before",
    "has_diff",
    "search",
)


@admin.register(Audit)
class AuditAdmin(admin.ModelAdmin):
    """Read-only admin for Audit records, sourced from the repository.

    Row data comes from ``AuditRepository.query`` / ``.get`` rather than the ORM,
    so the same admin renders a log held anywhere. The ModelAdmin provides only
    the registration shell: auth, permission checks, index entry, breadcrumbs.
    """

    change_list_template = "admin/vinta_audit_logs/audit/change_list.html"
    detail_template = "admin/vinta_audit_logs/audit/audit_detail.html"

    #: Resolved per request rather than held on the instance: a ModelAdmin is a
    #: long-lived singleton, and a repository that caches ids should not outlive
    #: the process's view of the database any longer than one request.
    def get_repository(self) -> AuditRepository | None:
        """The repository this admin reads through. Override in a subclass."""
        return get_audit_repository()

    # ------------------------------------------------------------------ #
    # Read-only enforcement                                               #
    # ------------------------------------------------------------------ #

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Deny adds -- the log is append-only, written through AuditService."""
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Deny changes -- audit records are immutable."""
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Deny deletes -- audit records are immutable."""
        return False

    # ------------------------------------------------------------------ #
    # Repository-backed changelist                                        #
    # ------------------------------------------------------------------ #

    def changelist_view(
        self,
        request: HttpRequest,
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        """Render the audit changelist from ``AuditRepository.query(...)``.

        Security: requires staff status, checked by the ModelAdmin view dispatch
        via ``admin_site.admin_view``. Non-staff requests are redirected to login
        before this method runs.
        """
        page = max(1, _parse_int(request.GET.get("page")) or 1)
        per_page = min(
            _MAX_PER_PAGE,
            max(1, _parse_int(request.GET.get("per_page")) or _DEFAULT_PER_PAGE),
        )
        offset = (page - 1) * per_page

        q = _build_audit_query(dict(request.GET))

        repository = self.get_repository()
        audit_page = repository.query(q, offset=offset, limit=per_page) if repository else None

        total = audit_page.total if audit_page is not None else 0
        num_pages = max(1, (total + per_page - 1) // per_page) if total > 0 else 1

        active_filters = {name: request.GET.get(name, "") for name in _FILTER_PARAMS}
        base_qs_params = {k: v for k, v in active_filters.items() if v}
        export_querystring = urlencode(base_qs_params)
        base_qs_params["per_page"] = str(per_page)

        context: dict[str, Any] = {
            **self.admin_site.each_context(request),
            "title": "Audit records",
            "audit_page": audit_page,
            "page": page,
            "per_page": per_page,
            "total": total,
            "num_pages": num_pages,
            "has_prev": page > 1,
            "has_next": page < num_pages,
            "prev_page": page - 1,
            "next_page": page + 1,
            "action_choices": [("", "All actions"), *self.get_audit_action_choices()],
            "actor_type_choices": [("", "All actor types"), *self.get_actor_type_choices()],
            "scope_type_choices": [("", "All scope types"), *ScopeType.choices],
            "has_diff_choices": [("", "Any"), ("yes", "Has diff"), ("no", "No diff")],
            "active_filters": active_filters,
            # urlencode so filter values containing & or other specials are safe.
            "base_querystring": urlencode(base_qs_params),
            "export_querystring": export_querystring,
            # The admin base template needs opts for breadcrumbs.
            "opts": self.model._meta,
            **(extra_context or {}),
        }
        return TemplateResponse(request, self.change_list_template, context)

    def get_audit_action_choices(self) -> Iterable[tuple[str, str]]:
        """Options for the *filter* dropdown, read from the action table.

        Named for the audit action rather than plain ``get_action_choices``:
        that name is already ``ModelAdmin``'s, for the bulk-actions menu, and
        overriding it here would quietly replace a piece of admin machinery with
        something of an entirely different shape.

        A query rather than an enum, because actions are rows: a project adds
        one by recording it, and the filter should show it without a deploy.
        """
        from vinta_audit_logs.models import AuditAction

        return list(AuditAction.objects.order_by("key").values_list("key", "name"))

    def get_actor_type_choices(self) -> Iterable[tuple[str, str]]:
        """Options for the actor-type filter, read from the identities in use.

        Same reasoning as the actions: a project defines its own actor kinds, so
        the list comes from what has actually been recorded.
        """
        from vinta_audit_logs.models_registry import get_audit_identity_model

        values = (
            get_audit_identity_model()
            .objects.order_by("identity_type")
            .values_list("identity_type", flat=True)
            .distinct()
        )
        return [(value, value) for value in values]

    # ------------------------------------------------------------------ #
    # URL routing                                                         #
    # ------------------------------------------------------------------ #

    def get_urls(self):
        """Register the detail and export views, wrapped for auth."""
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:audit_id>/view/",
                self.admin_site.admin_view(self.detail_view),
                name="vinta_audit_logs_audit_detail",
            ),
            path(
                "export/",
                self.admin_site.admin_view(self.export_view),
                name="vinta_audit_logs_audit_export",
            ),
        ]
        return custom_urls + urls

    # ------------------------------------------------------------------ #
    # Repository-backed detail view                                       #
    # ------------------------------------------------------------------ #

    def detail_view(
        self,
        request: HttpRequest,
        audit_id: int,
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        """Render a read-only detail page for a single audit record."""
        repository = self.get_repository()
        if repository is None:
            raise Http404("Audit record not found (repository unavailable).")

        record = repository.get(audit_id)
        if record is None:
            raise Http404(f"Audit record {audit_id} not found.")

        formatted_diff = [
            {"field": field_name, "old": changes.get("old"), "new": changes.get("new")}
            for field_name, changes in sorted((record.diff or {}).items())
        ]

        context: dict[str, Any] = {
            **self.admin_site.each_context(request),
            "title": f"Audit record #{record.id}",
            "record": record,
            "formatted_diff": formatted_diff,
            "actor_metadata": sorted((record.actor.metadata or {}).items()),
            "opts": self.model._meta,
            **(extra_context or {}),
        }
        return TemplateResponse(request, self.detail_template, context)

    # ------------------------------------------------------------------ #
    # CSV export view (streaming, memory-efficient)                      #
    # ------------------------------------------------------------------ #

    #: CSV columns, flattened from AuditRecord. Complex values (lists, dicts)
    #: are JSON-encoded; absent values become the empty string.
    CSV_HEADER = (
        "id",
        "uid",
        "created_at",
        "scope_type",
        "scope_key",
        "action_key",
        "actor_type",
        "actor_key",
        "actor_label",
        "actor_is_staff",
        "actor_groups",
        "actor_metadata",
        "on_behalf_of_type",
        "on_behalf_of_key",
        "subject_type",
        "subject_id",
        "subject_label",
        "affected",
        "diff",
    )

    def csv_row(self, record) -> list[Any]:
        """One CSV row for one record. Override to add or reshape columns."""
        return [
            record.id,
            str(record.uid),
            record.created_at.isoformat(),
            record.scope.scope_type,
            record.scope.scope_key,
            record.action_key,
            record.actor.identity_type,
            record.actor.identity_key,
            record.actor.identity_label,
            record.actor.is_staff,
            json.dumps(record.actor.group_names) if record.actor.group_names else "",
            json.dumps(record.actor.metadata) if record.actor.metadata else "",
            record.on_behalf_of.identity_type if record.on_behalf_of else "",
            record.on_behalf_of.identity_key if record.on_behalf_of else "",
            record.subject.subject_type,
            record.subject.subject_id,
            record.subject.subject_label,
            json.dumps(
                [
                    {"type": snapshot.identity_type, "key": snapshot.identity_key}
                    for snapshot in record.affected
                ]
            )
            if record.affected
            else "",
            json.dumps(record.diff) if record.diff is not None else "",
        ]

    def _csv_row_generator(
        self,
        repository: AuditRepository | None,
        q: AuditQuery,
        chunk_size: int = 1000,
    ) -> Generator[str]:
        """Yield CSV rows for the filtered records, header first.

        Streams through ``iter_records`` so memory is bounded by the chunk size
        rather than the result count -- and so the export does not re-count the
        whole filtered set once per page the way a paginated read would.
        """
        if repository is None:
            logger.error("_csv_row_generator: no repository available.")
            return

        class _Echo:
            """Pseudo-buffer whose write() returns the value instead of storing it."""

            def write(self, value: str) -> str:
                return value

        writer = csv.writer(_Echo())
        yield writer.writerow(self.CSV_HEADER)
        for record in repository.iter_records(q, chunk_size=chunk_size):
            yield writer.writerow(self.csv_row(record))

    def export_view(self, request: HttpRequest) -> HttpResponseBase:
        """Stream a CSV export of the currently filtered records.

        Respects every active filter on the changelist. Security: staff-only,
        enforced by ``admin_site.admin_view`` in :meth:`get_urls`.
        """
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])

        response = StreamingHttpResponse(
            self._csv_row_generator(self.get_repository(), _build_audit_query(dict(request.GET))),
            content_type="text/csv; charset=utf-8",
        )
        response["Content-Disposition"] = "attachment; filename=audit_export.csv"
        return response
