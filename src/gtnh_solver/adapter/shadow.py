"""Read a ShadowTheAge calculator plan (``.gtnh``) through gtnh-shadow-convert.

A ``.gtnh`` file holds hashed recipe ids, tiers and machine options, and nothing this adapter can
map: the recipes live in the calculator's ``data.bin`` and the rates come out of its solver. The
converter (MrBruh/gtnh-shadow-convert, MIT, a port of the calculator's reader, solver and machine
rules) resolves both and writes the plan in the shape :class:`~.plan.Plan` already validates, with a
``converter`` block that identifies it (:attr:`~.producer.PlanProducer.SHADOW_V1`)::

    plan.gtnh + data.bin --gtnh_shadow_convert.convert--> plan JSON --Plan.model_validate--> Plan

The converter is an optional extra (``pip install -e ".[shadow]"``), imported only here and only
when a ``.gtnh`` is read, so a plain install never needs it. Its own errors (an unknown recipe, a
plan that cannot be balanced, a machine whose rule is not ported) are ``ValueError``s, which the CLI
already reports as an unloadable export.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ._errors import AdapterError
from .plan import Plan

#: How to install the converter, for the error a missing one raises.
SHADOW_EXTRA_HINT = 'pip install -e ".[shadow]"'


def _require_converter() -> Callable[..., dict[str, Any]]:
    """The converter's ``convert``, imported lazily, or an :class:`AdapterError` saying how to
    install it."""
    try:
        from gtnh_shadow_convert import convert
    except ModuleNotFoundError as exc:
        raise AdapterError(
            f"reading a ShadowTheAge .gtnh plan needs gtnh-shadow-convert, the optional 'shadow' "
            f"extra: {SHADOW_EXTRA_HINT}"
        ) from exc
    converter: Callable[..., dict[str, Any]] = convert
    return converter


def load_shadow_plan(gtnh: str | Path, data: str | Path) -> Plan:
    """Convert the ``.gtnh`` plan at ``gtnh`` against the calculator data at ``data`` (its
    ``data.bin``, which ``gtnh-shadow-convert fetch-data`` downloads) and validate it as a
    :class:`Plan`."""
    convert = _require_converter()
    return Plan.model_validate(convert(gtnh, data))
