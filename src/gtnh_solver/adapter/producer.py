"""Provenance of an exported plan: which gtnh-factory-flow fork made it, against which pack.

Two live forks emit plans this solver can load, and they are **not** distinguishable by
``schemaVersion``: MrBruh's fork bumped to 2 when it added the ``resolved`` block, while
arodoid's kept 1 through a thousand diverging commits. So an arodoid plan loads with
``schema_version=1, resolved=None`` and no warning at all, then takes a power-sizing path that
understates its EU/t: measured against the plans' own per-tier figures, 6.1x low across a whole
plan and 256x low on one machine, because ``recipe.eut`` is the pre-overclock value and an EV
machine running an LV recipe draws 4^3 times it. Closing that silence is what this module is for.

Detection reads **structural markers that only one producer emits**, never the schema integer:

===================  ====================================================================
MrBruh fork          ``resolved`` or ``app`` present, or ``schemaVersion >= 2``
arodoid fork         ``recipes[].machineHandlers`` non-empty on any recipe
===================  ====================================================================

Resolution order::

    explicit pin given  -------------------------------------> that producer, no detection
    otherwise, markers of exactly one producer present ------> that producer
    otherwise (none present, or both) -----------------------> None

``None`` means *undetermined*, not *invalid*: the mapping still runs, but anything that keys off a
producer (the MrBruh-only ``_supply_tier`` workaround, provenance warnings) must abstain rather
than guess. A caller that knows better passes ``explicit``.

**Detection is silent by design.** An undetermined result is normal for any hand-built or minimal
plan - a synthetic test plan carries neither marker - so warning here would fire on most of the
suite and teach readers to tune ``AdapterWarning`` out, which would cost us the one warning that
matters (``core._check_power_provenance``). The advice for an undetermined *real* plan is to name
the fork on the command line, so ``cli`` is where that is reported.
"""

from __future__ import annotations

from enum import Enum

from .plan import Plan


class PlanProducer(str, Enum):
    """The gtnh-factory-flow fork an exported plan came from.

    ``str``-valued so the CLI can pass its ``--plan-schema`` choice straight through and so a
    warning or report can interpolate it without a cast.
    """

    #: ``MrBruh/gtnh-factory-flow``: carries the ``resolved`` throughput block the adapter trusts.
    MRBRUH_V2 = "mrbruh-v2"
    #: ``arodoid/gtnh-factory-flow``: no ``resolved`` block; carries ``machineHandlers`` and
    #: per-tier ``runtimeCalculation`` instead.
    ARODOID_V1 = "arodoid-v1"


def detect_producer(plan: Plan) -> PlanProducer | None:
    """The producer inferred from ``plan``'s structural markers, or ``None`` if undetermined.

    ``None`` covers both "no marker" (a minimal plan that happens to carry neither, e.g. one whose
    every recipe has an empty ``machineHandlers``) and "both markers", which would mean a fork has
    grown the other's field and this heuristic needs revisiting. Neither is an error: the caller
    warns and abstains.
    """
    mrbruh = plan.resolved is not None or plan.app is not None or plan.schema_version >= 2
    arodoid = any(recipe.machine_handlers for recipe in plan.recipes)
    if mrbruh and not arodoid:
        return PlanProducer.MRBRUH_V2
    if arodoid and not mrbruh:
        return PlanProducer.ARODOID_V1
    return None


def resolve_producer(plan: Plan, explicit: PlanProducer | None = None) -> PlanProducer | None:
    """``explicit`` if given, else :func:`detect_producer`. The one place the precedence lives.

    An explicit choice is taken on trust and never cross-checked against the markers: it exists
    precisely so a caller can override a detection that is wrong or impossible, and second-guessing
    the override the user just asked for would defeat the point of having it.
    """
    if explicit is not None:
        return explicit
    return detect_producer(plan)


def describe_markers(plan: Plan) -> str:
    """The detection evidence, for a caller reporting why a producer could not be determined."""
    return (
        f"schemaVersion={plan.schema_version}, "
        f"resolved={'present' if plan.resolved is not None else 'absent'}, "
        f"app={'present' if plan.app is not None else 'absent'}, "
        f"machineHandlers="
        f"{'present' if any(r.machine_handlers for r in plan.recipes) else 'absent'}"
    )


def strip_dataset_channel(dataset_version_id: str) -> str:
    """``"stable-2.8.4"`` -> ``"2.8.4"``, ``"local-2.9.0-beta-2"`` -> ``"2.9.0-beta-2"``.

    The exporter namespaces its recipe dataset by release channel; our ``data/<version>/`` folders
    are named by pack version alone, so the channel has to come off for the two to join. The rule is
    "drop a leading segment that is not itself part of the version", tested by whether what follows
    starts with a digit - which leaves an already-bare ``2.9.0-beta-2`` untouched, since ``beta-2``
    does not. Returns the input unchanged when there is nothing to strip.
    """
    head, _, tail = dataset_version_id.partition("-")
    if tail and not head[:1].isdigit() and tail[:1].isdigit():
        return tail
    return dataset_version_id


def plan_pack_version(plan: Plan) -> str | None:
    """The GTNH pack version this plan was balanced against, or ``None`` if it does not say one.

    Read from ``recipes[].source.datasetVersionId``, which both forks emit, with the channel
    stripped so it names a ``data/<version>/`` folder directly. ``None`` when no recipe carries one
    (some non-GregTech recipe kinds do not) **or when they disagree**: a plan spanning two datasets
    has no single answer, and picking a winner there would silently size a layout against the wrong
    pack, which is the failure this function exists to prevent.
    """
    versions = {
        strip_dataset_channel(recipe.source.dataset_version_id)
        for recipe in plan.recipes
        if recipe.source is not None and recipe.source.dataset_version_id
    }
    if len(versions) != 1:
        return None
    return versions.pop()
