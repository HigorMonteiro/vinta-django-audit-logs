"""The admin pages, actually rendered.

Every view here is repository-backed rather than ORM-backed, and every template
is this app's own. Both of those are exactly the kind of thing that passes a
unit test of the underlying call and still returns 500 to a person: a reverse
that names an app label, a context key a template reads and a view does not set.
So these tests render the responses and read the bytes.

The changelist regression in 0.1.2 is why: its breadcrumb reversed
``admin:app_list`` with the app label this code carried before it was extracted
into a package, which no installing project has. Nothing here rendered a page,
so nothing caught it.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from tests.conftest import record_data
from vinta_audit_logs.types import SubjectRef

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff_client(db) -> Client:
    """A logged-in superuser -- every audit view is behind ``admin_view``."""
    user = User.objects.create_superuser(
        username="staff",
        email="staff@example.com",
        password="password",
    )
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def one_record(service):
    """A single persisted record, for the views that need something to show."""
    return service.persist(
        record_data(
            scope_key="42",
            subject=SubjectRef(
                subject_type="testapp.article",
                subject_id="1",
                subject_label="Something that happened",
            ),
            diff={"title": {"old": "before", "new": "after"}},
        )
    )


class TestChangelist:
    def test_it_renders(self, staff_client, one_record) -> None:
        response = staff_client.get(reverse("admin:vinta_audit_logs_audit_changelist"))

        assert response.status_code == 200

    def test_the_breadcrumb_points_at_this_apps_index(self, staff_client) -> None:
        """The 0.1.2 regression, pinned.

        ``admin:app_list`` only reverses for a label the admin site has
        registered, so a hardcoded one that no longer exists raises
        ``NoReverseMatch`` and takes the whole page down with it.
        """
        response = staff_client.get(reverse("admin:vinta_audit_logs_audit_changelist"))

        assert response.status_code == 200
        assert (
            reverse("admin:app_list", kwargs={"app_label": "vinta_audit_logs"}).encode()
            in response.content
        )

    def test_it_shows_the_records_the_repository_returns(self, staff_client, one_record) -> None:
        response = staff_client.get(reverse("admin:vinta_audit_logs_audit_changelist"))

        assert b"No audit records found" not in response.content
        assert b"testapp.article" in response.content

    def test_it_renders_with_nothing_to_show(self, staff_client) -> None:
        """An empty log is the state every installation starts in."""
        response = staff_client.get(reverse("admin:vinta_audit_logs_audit_changelist"))

        assert response.status_code == 200
        assert b"No audit records found" in response.content

    def test_filters_reach_the_repository(self, staff_client, service) -> None:
        service.persist(record_data(scope_key="1"))
        service.persist(record_data(scope_key="2"))

        response = staff_client.get(
            reverse("admin:vinta_audit_logs_audit_changelist"), {"scope_key": "1"}
        )

        assert response.status_code == 200
        assert b"Showing 1 of 1 record" in response.content


class TestDetail:
    def test_it_renders(self, staff_client, one_record) -> None:
        response = staff_client.get(
            reverse("admin:vinta_audit_logs_audit_detail", args=[one_record.id])
        )

        assert response.status_code == 200
        assert b"Something that happened" in response.content

    def test_the_breadcrumb_points_at_this_apps_index(self, staff_client, one_record) -> None:
        response = staff_client.get(
            reverse("admin:vinta_audit_logs_audit_detail", args=[one_record.id])
        )

        assert (
            reverse("admin:app_list", kwargs={"app_label": "vinta_audit_logs"}).encode()
            in response.content
        )

    def test_it_shows_the_diff(self, staff_client, one_record) -> None:
        response = staff_client.get(
            reverse("admin:vinta_audit_logs_audit_detail", args=[one_record.id])
        )

        assert b"before" in response.content
        assert b"after" in response.content

    def test_an_unknown_record_is_a_404(self, staff_client) -> None:
        response = staff_client.get(reverse("admin:vinta_audit_logs_audit_detail", args=[999999]))

        assert response.status_code == 404


class TestExport:
    def test_it_streams_a_csv(self, staff_client, one_record) -> None:
        response = staff_client.get(reverse("admin:vinta_audit_logs_audit_export"))

        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/csv")
        body = b"".join(response.streaming_content)
        assert b"testapp.article" in body

    def test_it_honours_the_same_filters_as_the_changelist(self, staff_client, service) -> None:
        service.persist(record_data(scope_key="1"))
        service.persist(record_data(scope_key="2"))

        response = staff_client.get(
            reverse("admin:vinta_audit_logs_audit_export"), {"scope_key": "1"}
        )

        body = b"".join(response.streaming_content)
        # The header, plus the one row that survives the filter.
        assert len(body.strip().splitlines()) == 2


class TestReadOnly:
    """The log is append-only, written through the service. The admin is a
    reader, and the three write paths are closed at the permission layer rather
    than by hiding buttons."""

    def test_adding_is_denied(self, staff_client) -> None:
        response = staff_client.get(reverse("admin:vinta_audit_logs_audit_add"))

        assert response.status_code in (302, 403)

    def test_the_change_form_offers_nothing_to_save(self, staff_client, one_record) -> None:
        """Django renders the change view read-only rather than refusing it when
        ``has_change_permission`` is False and view permission is not -- so what
        there is to assert is that it carries no way to submit."""
        response = staff_client.get(
            reverse("admin:vinta_audit_logs_audit_change", args=[one_record.id])
        )

        assert response.status_code == 200
        assert b'name="_save"' not in response.content

    def test_posting_a_change_is_refused(self, staff_client, one_record) -> None:
        response = staff_client.post(
            reverse("admin:vinta_audit_logs_audit_change", args=[one_record.id]),
            data={"action_key": "tampered"},
        )

        assert response.status_code in (302, 403)

    def test_deleting_is_denied(self, staff_client, one_record) -> None:
        response = staff_client.get(
            reverse("admin:vinta_audit_logs_audit_delete", args=[one_record.id])
        )

        assert response.status_code in (302, 403)


class TestAuthentication:
    def test_an_anonymous_visitor_is_sent_to_the_login_page(self, client) -> None:
        """``admin_view`` wraps the two custom URLs too, not just the changelist."""
        for name, args in (
            ("admin:vinta_audit_logs_audit_changelist", []),
            ("admin:vinta_audit_logs_audit_export", []),
            ("admin:vinta_audit_logs_audit_detail", [1]),
        ):
            response = client.get(reverse(name, args=args))

            assert response.status_code == 302
            assert "/login/" in response["Location"]
