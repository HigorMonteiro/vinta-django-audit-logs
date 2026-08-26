from __future__ import annotations

from django.apps import AppConfig


class SwappedConfig(AppConfig):
    name = "tests.swapped"
    label = "swapped"
