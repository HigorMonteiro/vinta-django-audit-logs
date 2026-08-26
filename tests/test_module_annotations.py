"""Every module's annotations must survive being read.

Python 3.14 evaluates a function's annotations the first time anything reads
``__annotations__``, and plenty of things do: Django's own introspection, admin
autodiscovery, and every dependency-injection library that inspects a callable's
signature. A name that exists only inside ``if TYPE_CHECKING:`` is not bound when
that happens, and the module blows up with a ``NameError`` far from the import
that caused it.

Nothing about that is caught by importing a module, calling its functions, or
type-checking it -- which is exactly how it shipped in 0.1.0. This walks every
module in the package and reads what a real host project would read.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest

import vinta_audit_logs


def _module_names() -> list[str]:
    """Every importable module in the package, migrations aside.

    ``tasks`` is skipped: importing it requires Celery *and* ``AUDIT_CELERY_APP``,
    which is the whole point of it being the only module that does.
    """
    return [
        name
        for _finder, name, _ispkg in pkgutil.walk_packages(
            vinta_audit_logs.__path__, prefix="vinta_audit_logs."
        )
        if ".migrations" not in name and not name.endswith(".tasks")
    ]


@pytest.mark.parametrize("module_name", _module_names())
def test_annotations_can_be_read(module_name):
    """Reading a module's annotations must not raise."""
    module = importlib.import_module(module_name)

    for _name, obj in vars(module).items():
        if not (inspect.isfunction(obj) or inspect.isclass(obj)):
            continue
        if getattr(obj, "__module__", None) != module_name:
            continue
        # The read is the test: on 3.14 this is what evaluates them.
        annotations = getattr(obj, "__annotations__", {})
        assert isinstance(annotations, dict)
        for member in vars(obj).values():
            if inspect.isfunction(member):
                assert isinstance(getattr(member, "__annotations__", {}), dict)
