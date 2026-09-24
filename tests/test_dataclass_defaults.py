"""No dataclass in the package may default a field to an unhashable value (#255).

``dataclasses`` refuses a mutable default when the class is *created*, so breaking this rule is an
import error, and every entry point imports the router. The rule is not the same on every
interpreter, though, and CI tests only the floor and the newest release:

- 3.10 refuses only a ``list``, ``dict`` or ``set``;
- 3.11 refuses any default whose type's ``__hash__`` is ``None``, which includes ``mappingproxy``;
- 3.12+ uses the 3.11 rule, but ``mappingproxy`` gained a ``__hash__`` that delegates to the
  mapping it wraps, so ``MappingProxyType({})`` passes the check and only fails if it is hashed.

That is how ``MappingProxyType({})`` defaults shipped: every tested version accepted them and the
package could not be imported on 3.11. Running 3.11 would find the next one only if CI had a 3.11
leg, so this checks the defaults themselves instead: ``hash(default)`` fails for everything 3.11
refuses, and for a proxy over an unhashable mapping too, on every interpreter.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import pkgutil

import gtnh_solver


def _package_dataclasses() -> list[type]:
    """Every dataclass defined in ``gtnh_solver``, found by importing each of its modules."""
    found: dict[str, type] = {}
    for info in pkgutil.walk_packages(gtnh_solver.__path__, prefix="gtnh_solver."):
        module = importlib.import_module(info.name)
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if dataclasses.is_dataclass(obj) and obj.__module__.startswith("gtnh_solver."):
                found[f"{obj.__module__}.{obj.__qualname__}"] = obj
    return list(found.values())


def test_the_package_has_dataclasses_to_check() -> None:
    # Guards the test below against passing vacuously if the walk ever stops finding anything.
    names = {cls.__name__ for cls in _package_dataclasses()}
    assert {"AutoAssignment", "RouteResult"} <= names


def test_every_dataclass_default_is_hashable() -> None:
    unhashable = []
    for cls in _package_dataclasses():
        for f in dataclasses.fields(cls):
            if f.default is dataclasses.MISSING:
                continue
            try:
                hash(f.default)
            except TypeError:
                unhashable.append(f"{cls.__module__}.{cls.__qualname__}.{f.name}")
    assert not unhashable, f"use field(default_factory=...) for: {unhashable}"
