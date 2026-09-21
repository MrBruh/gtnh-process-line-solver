"""The item pipe capacity rule a route's size is chosen from (#165).

Two kinds of figure live in ``dataset/pipe_capacity.py`` and they are tested differently, because
they are trusted for different reasons:

1. **GT's figures** (insertions per window, one stack per insertion) are transcriptions of
   ``LoaderMetaPipeEntities.ItemPipeBuilder``. The table is re-derived here from the builder's own
   arithmetic, so a typo in one row cannot hide behind a comment that agrees with it.
2. **The calibration** (how often an endpoint must be served) is not a GT figure. It is pinned to
   the one in-game measurement there is, so a change to it has to come with a reason.
"""

from __future__ import annotations

import pytest

from gtnh_solver.dataset import (
    ITEM_PIPE_CAPACITY,
    ITEMS_PER_INSERTION,
    ROUTED_PIPE_SIZES,
    STREAM_SERVICE_TICKS,
    endpoint_insertions,
    item_pipe_insertions,
    item_pipe_size_for,
)
from gtnh_solver.ir import Commodity, PipeSize

#: Tin's ``invSlotsForHugePipe`` (``LoaderMetaPipeEntities.registerItemPipes``).
_TIN_HUGE_SLOTS = 2

#: The divisor ``ItemPipeBuilder.build`` scales each smaller size by.
_DIVISOR = {PipeSize.TINY: 16, PipeSize.SMALL: 8, PipeSize.NORMAL: 4, PipeSize.LARGE: 2}


def _gt_builder(size: PipeSize, huge_slots: int) -> tuple[int, int]:
    """``(slots, window ticks)`` exactly as ``ItemPipeBuilder.build`` computes them, in Java ints."""
    if size is PipeSize.HUGE:
        return huge_slots, 20  # MTEItemPipe's shorter constructor passes a 20-tick window
    d = _DIVISOR[size]
    return max(huge_slots // d, 1), max(d // huge_slots, 1) * 20


@pytest.mark.parametrize("size", list(PipeSize))
def test_the_capacity_table_is_gts_builder_arithmetic(size: PipeSize) -> None:
    assert ITEM_PIPE_CAPACITY[size] == _gt_builder(size, _TIN_HUGE_SLOTS)


def test_every_size_on_the_ladder_has_a_capacity() -> None:
    assert set(ITEM_PIPE_CAPACITY) == set(PipeSize)


def test_capacity_grows_with_size() -> None:
    """The ladder is only a ladder if each step moves more; the lookup relies on it."""
    rates = [item_pipe_insertions(size) for size in PipeSize]
    assert rates == sorted(rates)
    assert len(set(rates)) == len(rates)


def test_insertions_per_service_interval() -> None:
    """Scaled from GT's own windows to the one interval the demand is counted in."""
    assert STREAM_SERVICE_TICKS == 40  # the plain pipe's own window: see the next test
    assert [item_pipe_insertions(size) for size in PipeSize] == [0.25, 0.5, 1.0, 2.0, 4.0]


def test_the_calibration_is_the_one_measurement_there_is() -> None:
    """A plain tin pipe fed one hammer of three in game. The calibration must reproduce exactly
    that - enough for one endpoint, not for two - or it is not the measurement any more."""
    plain = item_pipe_insertions(PipeSize.NORMAL)
    assert plain >= endpoint_insertions(0.1)
    assert plain < 2 * endpoint_insertions(0.1)


@pytest.mark.parametrize(
    ("rate", "insertions"),
    [
        (0.0, 1),  # an endpoint that moves nothing is still an endpoint to reach
        (0.1, 1),  # a Forge Hammer on the sand line
        (0.30000000000000004, 1),  # float noise from the adapter must not round up
        (1.6, 1),  # exactly one stack per interval
        (1.61, 2),  # a fraction over spills into a second insertion
        (3.2, 2),
        (3.3, 3),
    ],
)
def test_an_endpoint_needs_one_insertion_per_stack_it_moves(rate: float, insertions: int) -> None:
    assert endpoint_insertions(rate) == insertions


def test_a_stack_is_gts_sixty_four() -> None:
    assert ITEMS_PER_INSERTION == 64


@pytest.mark.parametrize(
    ("demand", "size"),
    [
        (1, PipeSize.NORMAL),
        (2, PipeSize.LARGE),
        (3, PipeSize.HUGE),
        (4, PipeSize.HUGE),
        (5, None),  # past the largest tin pipe: a faster material, which is not chosen here
    ],
)
def test_the_smallest_size_that_carries_the_demand(demand: int, size: PipeSize | None) -> None:
    assert item_pipe_size_for(demand) is size


def test_the_router_never_lays_a_size_that_cannot_reach_one_endpoint() -> None:
    """Tiny and small make fewer than one insertion per interval, so no demand ever picks them,
    and the committed manifest need not carry them."""
    assert ROUTED_PIPE_SIZES[Commodity.ITEM] == (PipeSize.NORMAL, PipeSize.LARGE, PipeSize.HUGE)
    assert ROUTED_PIPE_SIZES[Commodity.FLUID] == (PipeSize.NORMAL,)
