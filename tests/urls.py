"""URLs for the test project -- the admin, which the app registers into."""

from __future__ import annotations

from django.contrib import admin
from django.urls import path

urlpatterns = [path("admin/", admin.site.urls)]
