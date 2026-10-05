"""Input IR - the *problem* the solver consumes.

Produced by the adapter from a gtnh-factory-flow exported plan JSON (recipes embedded)
plus the physical-rules dataset. Spec: docs/IR.md. This is one of two versioned
contracts everything couples to, so it is kept minimal and grown with explicit version
bumps (see ``INPUT_IR_VERSION`` and the changelog in ``__init__.py``).

What this contract guarantees (checked here) vs. what it does NOT:
- Guaranteed: structural well-formedness + *referential integrity* - unique ids, every
  net/pinned reference resolves to an existing machine+port, a net's commodity matches
  the ports it touches. Downstream code may assume these hold.
- NOT checked here: geometric/rule validity (cells in-bounds, no machine overlaps,
  throughput within tier caps, required-face reachability). That is the validator's job,
  on purpose - it has independent logic so it can catch solver bugs (docs/TESTING.md).
"""

from __future__ import annotations

import math
import re

from pydantic import Field, field_validator, model_validator

from ._base import FrozenModel, StrictModel, check_contract_version
from .enums import HORIZONTAL_FACINGS, Commodity, Facing, IODirection, RelativeFace
from .geometry import CellBox, CellCoord, allowed_faces
from .me import MEConfig, MEEndpoint, MERole

#: Bump on any breaking change to the input contract; record it in ``ir/__init__.py``.
INPUT_IR_VERSION = 9

#: What :attr:`InputIR.resource_colors` holds, once lowercased: ``#`` and six hex digits.
_HEX_COLOR = re.compile(r"#[0-9a-f]{6}")


class Port(StrictModel):
    """One required I/O point the solver must expose on a usable machine face.

    The *physical* face is chosen by the solver (placement + orientation); this only states
    the requirement, and which faces are usable (:attr:`faces`, read through
    :meth:`Machine.allowed_faces`). Whether a port is satisfied by auto-output is a **solver
    decision**, not a problem input - it is recorded in the output's ``AutoConnection`` (and the
    validator enforces one auto-output per machine there), so it is deliberately not a field here.
    """

    id: str = Field(min_length=1)
    commodity: Commodity
    direction: IODirection
    #: Reserved: a per-port cover override (conveyor for items, pump/regulator for fluids). Not
    #: yet produced or consumed. The text build guide derived a cover from the commodity at render
    #: time until it was retired (#203), and no surface names one since; this stays ``None`` until
    #: a dataset sets the specific cover a port needs (e.g. a regulator vs a plain pump).
    cover: str | None = None
    #: Throughput through this port - items/t, mB/t, or (since IR v3) **EU/t for a power port**.
    #: ``None`` when unknown. The adapter fills it from the recipe; it surfaces boundary I/O rates
    #: (``system_io``, previewer). For power it is the share of the machine's ``eut`` that arrives
    #: through *this* connection: a multiblock spreads its draw over several energy hatches, so the
    #: router and validator size a cable from the port's rate, never the whole machine's.
    rate: float | None = Field(default=None, ge=0.0)
    #: Most amps this single connection can accept. A GT energy hatch takes 2 A
    #: (``dataset.ENERGY_HATCH_AMPS``). It is what a connection can take **in**, so with the
    #: delivered voltage it says how much power actually reaches the machine; a cable offering
    #: more is not an error (the hatch just takes its 2), but hatches that together take in less
    #: than the machine's ``eut`` mean it cannot run its recipe.
    #:
    #: ``None`` means the ceiling is **unknown**, not unlimited: every GT connection has one, the
    #: producer just could not name the rule that gives it (``adapter.power`` states a hatch's 2 A
    #: or a basic machine's ``maxAmperesIn``, and abstains where the machine's class is not
    #: established). A consumer must therefore not read ``None`` as "satisfied" - the validator
    #: treats the machine's intake as unmeasurable and reports it
    #: (``ValidationReport.unverified_power_intake``).
    max_amps: float | None = Field(default=None, gt=0.0)
    #: The only faces this port may dock on, named from the machine's point of view so they turn
    #: with it (InputIR v4). ``None``, the default, is the rule every machine has always had: any
    #: face but the front, which carries no I/O. A pin states a block whose faces do fixed jobs: an
    #: Item Filter takes items on every face but its back (its front included) and pushes them out
    #: of its back and nowhere else (``MTEBuffer``), so its output port is pinned to ``(back,)``.
    #: Read it through :meth:`Machine.allowed_faces`, never directly, so there is one reading of it.
    faces: tuple[RelativeFace, ...] | None = None
    #: The output layer GT fills this port's fluid from, on a machine that fills its outputs by layer
    #: (InputIR v6, #299): GT's own per-layer list index, which is the fluid's place among the
    #: recipe's fluid outputs. A Distillation Tower hands recipe fluid output ``i`` to the hatches on
    #: its ``i``-th layer above the base and to no others, so this port's hatch may stand only on a
    #: slot of that layer (:meth:`Machine.hatch_slots_for`). ``None`` everywhere else: an item, power
    #: or input port, and any port of a machine that fills its outputs first fit. Only a fluid
    #: output may carry one, and it must name a layer the machine's slots record.
    output_layer: int | None = Field(default=None, ge=0)

    @field_validator("faces")
    @classmethod
    def _check_faces(
        cls, value: tuple[RelativeFace, ...] | None
    ) -> tuple[RelativeFace, ...] | None:
        if value is None:
            return value
        if not value:
            raise ValueError("a pinned port names at least one face (None means unpinned)")
        if len(value) != len(set(value)):
            raise ValueError("a port's faces must not repeat")
        return value

    @model_validator(mode="after")
    def _check_output_layer(self) -> Port:
        if self.output_layer is not None and (
            self.commodity is not Commodity.FLUID or self.direction is not IODirection.OUTPUT
        ):
            raise ValueError(
                f"only a fluid output port may name an output layer; {self.id!r} is a "
                f"{self.commodity.value} {self.direction.value}"
            )
        return self


class FaceSpec(StrictModel):
    """The catalog of I/O ports a machine needs across its usable faces.

    Not a fixed face->port map: face assignment is a solver decision, within whatever faces a port
    is pinned to (``Port.faces``). An unpinned port never takes the front face (set by
    orientation), which carries no I/O.
    """

    ports: list[Port] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> FaceSpec:
        ids = [p.id for p in self.ports]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate port id within a machine's FaceSpec")
        return self


class HatchSlot(FrozenModel):
    """One casing cell of a multiblock that can host a hatch or bus, and the kinds it accepts.

    ``offset`` is measured from the machine's **unrotated** minimum corner, the same corner
    ``Placement.cell`` names, so a placed slot's world cell is
    ``placement.cell + ir.geometry.rotated_slot(offset, footprint, placement.orientation)``. Kept
    unrotated here because orientation is a placement decision the IR must not pre-empt.

    ``kinds`` holds ``gregtech.api.enums.HatchElement`` names (``OutputHatch``, ``InputBus``,
    ``Energy``, ``Maintenance``, ``Muffler``, ...), sorted so a layout is reproducible. **It is a
    lower bound, never a whitelist**: a GT hatch adder built from a bare method reference exposes no
    filter, so a dump taken before #227 records its cell without that kind rather than wrongly. In
    the 2.8.4 dump 23 of 208 controllers record no slots at all, 61 of the remaining 185 record no
    ``Energy``-capable cell, and 35 no ``Maintenance``-capable one; a 2.9 dump taken since still has
    98 of 286 with no ``Energy`` cell. A consumer that treats an absent kind as a prohibition
    manufactures a false infeasibility across roughly a third of the dataset; treat "unrecorded" as
    permissive (``validator/core`` already refuses to enforce per-kind counts for this reason).
    """

    offset: CellCoord
    kinds: tuple[str, ...] = Field(min_length=1)
    #: Which of the machine's per-layer output lists an output hatch here is filed under (InputIR
    #: v6, #299), as the dump recorded it (dataset schema v3): GT's own index, from 0, which is the
    #: recipe fluid output the hatch receives. ``None`` for a cell in no list: one that takes no
    #: output hatch, any cell of a machine that fills its outputs first fit, and a tower's top centre
    #: or base. A tower forms only with an output hatch on every layer (:attr:`Machine.output_layers`).
    output_layer: int | None = Field(default=None, ge=0)


class StructureBlock(FrozenModel):
    """The block one tiered part of a multiblock is built from (InputIR v7, #312).

    A GT multiblock names some parts by a StructureLib channel rather than by a block (a coil, a
    solid casing, a pipe casing, a machine casing), and the tier it is built from decides whether,
    or how fast, the machine runs. ``block`` is the registry name and ``meta`` the block meta, the
    identity a structure dump and the texture manifest share.
    """

    block: str = Field(min_length=1)  # registry name, e.g. "gregtech:gt.blockcasings4"
    meta: int = Field(default=0, ge=0)


#: Which ``gregtech.api.enums.HatchElement`` kinds could host a port's hatch, by what the port
#: carries. The bus/hatch split is lexical in GT and means items/fluids: ``InputBus`` takes items
#: (``MTEHatchInputBus``), ``InputHatch`` fluids (``MTEHatchInput``). Power input is the one
#: many-to-one entry - a plain ``Energy`` hatch, TecTech's ``ExoticEnergy``, and ``MultiAmpEnergy``
#: all satisfy it, and 34 of the 208 dumped controllers record only ``ExoticEnergy``, so demanding
#: ``Energy`` alone would refuse a machine that plainly does accept power.
#:
#: **Not a closed vocabulary.** Roughly 30 further ``IHatchElement`` implementations exist outside
#: this enum (TecTech's ``EnergyMulti``/``DynamoMulti``/``InputData``, gtPlusPlus's own set,
#: per-controller ones like the driller's ``DataAccess``, and ``HatchElementEither``'s "A or B"),
#: any of which a dump may record. That is a second reason the lookup must stay permissive rather
#: than treating an unmatched kind as a prohibition; see :meth:`Machine.hatch_slots_for`.
HATCH_KINDS: dict[tuple[Commodity, IODirection], tuple[str, ...]] = {
    (Commodity.ITEM, IODirection.INPUT): ("InputBus",),
    (Commodity.ITEM, IODirection.OUTPUT): ("OutputBus",),
    (Commodity.FLUID, IODirection.INPUT): ("InputHatch",),
    (Commodity.FLUID, IODirection.OUTPUT): ("OutputHatch",),
    (Commodity.POWER, IODirection.INPUT): ("Energy", "ExoticEnergy", "MultiAmpEnergy"),
    (Commodity.POWER, IODirection.OUTPUT): ("Dynamo",),
}


class Machine(StrictModel):
    """A single machine to place at one position.

    Multi-instance machine groups (the gtnh-factory-flow balance can call for N identical
    copies of a recipe) are **not modelled yet**: a net endpoint (``MachineFaceRef``) cannot
    address one instance of a group, so the placer/router/validator could only drop the copies
    and leave the extras silently unwired. Until instance-aware routing exists (Phase 2,
    docs/ROADMAP.md) each ``Machine`` is exactly one instance, and the adapter rejects an export
    ``machineCount > 1`` rather than emit an under-wired layout. (``count`` was dropped in
    InputIR v1; see ``ir/__init__.py``.)
    """

    id: str = Field(min_length=1)
    type: str = Field(min_length=1)  # GT machine id; keys into the physical-rules dataset
    #: The GT controller block this machine is, as ``"<registry_name>@<meta>"``
    #: ("gregtech:gt.blockmachines@998"). ``type`` is the exporter's localized recipe-map name,
    #: which for GT++ machines differs from the controller block's name that the structure dataset
    #: is keyed by, and can even name a different controller ("Distillation Tower" for a Dangote
    #: Distillus). So this is the controller the adapter RESOLVED in the dataset, however it found
    #: it (the export's block id, a handler label, the recipe-map name or an alias), and the
    #: footprint and hatch slots come from that same record; consumers that draw the machine join
    #: on it exactly. Without a resolved record (no dataset, or a dump miss) it is the export's
    #: own key, or None when the export carried none; a consumer whose lookup finds nothing under
    #: it falls back to ``type``. (GitHub #98, #205.)
    block_key: str | None = None
    #: The GT recipe map this machine runs, by its unlocalized id (``"gt.recipe.orewasher"``), when
    #: the export states it. ``type`` is that map's LOCALIZED name, which is often not the machine's
    #: own ("Ore Washer" runs in a "Basic Ore Washing Plant") and not even unique (the Furnace's map
    #: and the Microwave's both localize to "Furnace"). So a consumer drawing a single-block machine
    #: joins on this and ``voltage_tier`` first, and falls back to ``type`` when it is None or finds
    #: nothing. (GitHub #232.)
    recipe_map: str | None = None
    footprint: CellBox = Field(default_factory=CellBox)
    faces: FaceSpec = Field(default_factory=FaceSpec)
    voltage_tier: str = Field(min_length=1)  # LV/MV/HV/... - sets cable voltage rating
    orientation_options: list[Facing] = Field(min_length=1)
    #: EU/t this machine draws; with ``voltage_tier`` it sets the amperage it pulls on a
    #: shared-amperage cable (dataset.amperage). 0 for an unpowered block or a power source.
    eut: float = Field(default=0.0, ge=0.0)
    #: How many cells of this machine's structure can hold a hatch/bus, or ``None`` when unknown
    #: (a single-block machine, or a plan adapted without the physical dataset). A multiblock's
    #: casing cells accept I/O of any kind - items, fluids, **or power** - so this is the ceiling
    #: on its total connections, energy hatches included. The validator uses it to reject a layout
    #: that wires more connections onto a machine than its structure has cells to host.
    hatch_cells: int | None = Field(default=None, ge=0)
    #: Where those cells actually are, when the structure dump recorded them. Empty for a
    #: single-block machine, for a plan adapted without the physical dataset, and for the 23 of 208
    #: controllers whose adders expose no filter - all of which read as "unknown", not "none", so a
    #: consumer falls back to treating any body face as dockable rather than refusing to place.
    #: ``len(hatch_slots)`` agrees with :attr:`hatch_cells` whenever both are present; the count
    #: exists separately because it survived a dump that recorded no offsets.
    hatch_slots: tuple[HatchSlot, ...] = ()
    #: The items this block lets through, when it is an Item Filter the adapter placed to sort a
    #: single block's merged item outputs (InputIR v4). Empty for every other machine. It is what
    #: the filter's nine slots are set to in game (``MTEFilter.allowPutStack`` refuses anything
    #: else), so the validator checks the filter's output against it and a builder reads it to
    #: configure the block.
    filter_items: tuple[str, ...] = ()
    #: Whether this machine stands for something built OUTSIDE the layout, which it faces with its
    #: front (InputIR v5): a Crop Manager, the block a crop card's output comes from, whose field is
    #: not part of the build (#282). Like a power source's feed face, that front lies flush on the
    #: region boundary. A power source needs no flag (:attr:`fronts_outside` reads its port).
    outside_front: bool = False
    #: The block each tiered part of this machine is built from, keyed by GT's channel id as the
    #: structure dump names it (``"coil"``, ``"casing"``, ``"pipe"``, ``"machine_casing"``;
    #: InputIR v7, #312). The adapter chooses them from the plan: the node's coil on any multiblock
    #: with a coil channel, and on a Chemical Plant the solid casing its recipes' special value
    #: needs, the pipe casing the plan names and the machine casing of the tier it is supplied at.
    #: A channel this does not name is built as the dump draws it. Empty for a single block, for a
    #: plan adapted without the physical dataset, and for a multiblock with no tiered part.
    structure_blocks: dict[str, StructureBlock] = Field(default_factory=dict)
    #: The ME devices this machine needs built (InputIR v9, #333): one per AE2 part or GT ME hatch
    #: that serves its ports on a net riding ME, and a link's one storage bus. Empty for a machine
    #: with no port on ME, and for every machine until the end-to-end build places them (#335).
    me_endpoints: tuple[MEEndpoint, ...] = ()
    #: What this machine is when it is ME infrastructure rather than a machine of the line: an
    #: attach stub, a link, a controller or an acceptor (InputIR v9). ``None`` for every other.
    me_role: MERole | None = None
    #: The ME network an infrastructure machine belongs to (``InputIR.me``), set exactly when
    #: :attr:`me_role` is.
    me_network: str | None = Field(default=None, min_length=1)

    @property
    def fronts_outside(self) -> bool:
        """Whether this machine's front faces outside the build, so placement puts it flush on the
        region boundary and the validator holds it there: a power source's feed face, or a machine
        flagged :attr:`outside_front`. The one reading of that rule, for every stage."""
        return self.outside_front or self.is_power_source

    @field_validator("filter_items")
    @classmethod
    def _check_filter_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value):
            raise ValueError("a filter item must name something")
        if len(value) != len(set(value)):
            raise ValueError("a filter's items must not repeat")
        return value

    @field_validator("structure_blocks")
    @classmethod
    def _check_structure_blocks(cls, value: dict[str, StructureBlock]) -> dict[str, StructureBlock]:
        if any(not channel for channel in value):
            raise ValueError("a structure block's channel must name something")
        return value

    def allowed_faces(self, port_id: str, orientation: Facing) -> frozenset[Facing]:
        """The world faces ``port_id`` may dock on while this machine faces ``orientation``.

        The ONE reading of the face rule, which the router, the placement search, the crowding gate
        and the validator all take from here: an unpinned port may use any face but the front
        (which carries no I/O), and a pinned one exactly the faces ``Port.faces`` names, turned with
        the machine (``ir.geometry.allowed_faces``). An unknown ``port_id`` gets the unpinned rule,
        the permissive reading the rest of this model gives a port it does not know.
        """
        for port in self.faces.ports:
            if port.id == port_id:
                return allowed_faces(port.faces, orientation)
        return allowed_faces(None, orientation)

    @property
    def is_power_source(self) -> bool:
        """Whether this machine *supplies* power (it has a power OUTPUT port).

        Today only the adapter's synthesized per-tier source matches (a plan export has no power
        nodes). Such a machine is fed externally by the builder: its front face is the reserved
        feed face and placement pins that face on the region boundary (validator-enforced), so
        power enters from outside the structure. When real in-plan generators arrive with the
        dataset lane, this structural predicate needs a dataset-driven refinement.
        """
        return any(
            p.commodity is Commodity.POWER and p.direction is IODirection.OUTPUT
            for p in self.faces.ports
        )

    @property
    def output_layers(self) -> frozenset[int]:
        """The output layers this machine's slots record, empty for one that fills first fit.

        A machine with any is a tower that fills its fluid outputs by layer, and GT forms it only
        with an output hatch on each of them, a layer no product uses included (#299).
        """
        return frozenset(s.output_layer for s in self.hatch_slots if s.output_layer is not None)

    @property
    def power_input_ports(self) -> list[Port]:
        """This machine's power INPUT ports - its energy hatches, in declaration order."""
        return [
            p
            for p in self.faces.ports
            if p.commodity is Commodity.POWER and p.direction is IODirection.INPUT
        ]

    def hatch_slots_for(self, port_id: str) -> tuple[HatchSlot, ...] | None:
        """The casing cells that may host ``port_id``'s hatch, or ``None`` when nothing is known.

        Three levels, because :attr:`HatchSlot.kinds` is a **lower bound, never a whitelist**:

        - this machine records no slots at all -> ``None``. The caller must fall back to treating
          every body cell as a candidate, which is what a single-block machine, a plan adapted
          without the dataset, and 23 of 208 controllers all need;
        - some slot accepts one of the port's kinds -> exactly those slots. This is the real
          constraint, and it is what makes a Distillation Tower's 17 output-only upper cells refuse
          an input hatch;
        - slots are recorded but *none* names the port's kind -> all of them. The dump is silent
          about that kind on this machine rather than prohibiting it: a hatch adder built from a
          bare method reference exposes no filter, so a dump taken before #227 records its cell
          without the kind. In the 2.8.4 dump 61 of 185 controllers record no ``Energy`` cell, the
          Chemical Plant among them, and reading that as a prohibition would refuse to power a
          machine that certainly takes power.

        An unknown ``port_id`` names no kinds and therefore lands in the third case, permissive.

        A port with an :attr:`Port.output_layer` is the exception, and a strict one: exactly the
        slots of its kind on that layer, with no fallback (#299). GT fills a tower by layer, so a
        hatch anywhere else receives a different product or none, and an empty answer means no cell
        can host the port, which the router reports rather than docking it on the wrong layer.
        """
        if not self.hatch_slots:
            return None
        kinds = frozenset(self.hatch_kinds_for(port_id))
        layer = self._output_layer(port_id)
        if layer is not None:
            return tuple(
                s
                for s in self.hatch_slots
                if s.output_layer == layer and not kinds.isdisjoint(s.kinds)
            )
        matching = tuple(s for s in self.hatch_slots if not kinds.isdisjoint(s.kinds))
        return matching or self.hatch_slots

    def _output_layer(self, port_id: str) -> int | None:
        """The output layer ``port_id`` names, or None for an unknown port or one with no layer."""
        for port in self.faces.ports:
            if port.id == port_id:
                return port.output_layer
        return None

    def hatch_kinds_for(self, port_id: str) -> tuple[str, ...]:
        """The ``HatchElement`` kinds that could host ``port_id``, empty for an unknown port."""
        for port in self.faces.ports:
            if port.id == port_id:
                return HATCH_KINDS.get((port.commodity, port.direction), ())
        return ()

    def port_eut(self, port_id: str) -> float:
        """EU/t arriving through ``port_id``: its own ``rate``, else the machine's whole ``eut``.

        The fallback keeps a single-port machine (and every pre-v3 problem, where power ports
        carried no rate) sizing exactly as it did, while a machine whose draw is spread over
        several energy hatches charges each cable only its own hatch's share.
        """
        for port in self.faces.ports:
            if port.id == port_id:
                return self.eut if port.rate is None else port.rate
        return 0.0

    @model_validator(mode="after")
    def _check(self) -> Machine:
        if len(self.orientation_options) != len(set(self.orientation_options)):
            raise ValueError("duplicate orientation in orientation_options")
        non_horizontal = [f for f in self.orientation_options if f not in HORIZONTAL_FACINGS]
        if non_horizontal:
            raise ValueError(
                "machine front must face a horizontal direction (N/S/E/W); "
                f"got {[f.value for f in non_horizontal]}"
            )
        rated = [p.rate for p in self.power_input_ports if p.rate is not None]
        if rated:
            # Split draws must account for the whole machine: a hatch left off the books would be
            # a cable nothing sizes, and an extra one would double-charge the net. Rates only ever
            # come from a division of ``eut``, so exact-ish equality is the right check.
            if len(rated) != len(self.power_input_ports):
                raise ValueError(
                    "power input ports must all carry a rate or none of them (a partially rated "
                    "machine would leave part of its draw unsized)"
                )
            total = math.fsum(rated)
            if not math.isclose(total, self.eut, rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError(
                    f"power input port rates sum to {total} EU/t but the machine draws {self.eut}"
                )
        self._check_output_layers()
        self._check_me()
        return self

    def _check_me(self) -> None:
        """An endpoint serves ports this machine has, under an id of its own, and an infrastructure
        machine names its network; an attach stub and a link face outside the build."""
        ids = [e.id for e in self.me_endpoints]
        if len(ids) != len(set(ids)):
            raise ValueError(f"machine {self.id!r} has two ME endpoints with one id")
        ports = {p.id for p in self.faces.ports}
        for endpoint in self.me_endpoints:
            if (not endpoint.ports) is (self.me_role is None):
                raise ValueError(
                    f"ME endpoint {endpoint.id!r} of {self.id!r}: a machine's endpoint serves its "
                    f"ports, and only an infrastructure block's (a link's storage bus) serves none"
                )
            unknown = [port for port in endpoint.ports if port not in ports]
            if unknown:
                raise ValueError(
                    f"ME endpoint {endpoint.id!r} of {self.id!r} serves unknown port(s) {unknown}"
                )
        if (self.me_role is None) is not (self.me_network is None):
            raise ValueError("an ME infrastructure machine names its network, and only it does")
        if self.me_role in (MERole.ATTACH, MERole.LINK) and not self.outside_front:
            raise ValueError(
                f"an ME {self.me_role.value} faces outside the build, so it must be outside_front"
            )

    def _check_output_layers(self) -> None:
        """A port's layer must be one the slots record, and a layered machine names every fluid
        output's: a tower's output with no layer would dock on any of them (#299)."""
        layers = self.output_layers
        for port in self.faces.ports:
            if port.output_layer is not None and port.output_layer not in layers:
                raise ValueError(
                    f"port {port.id!r} names output layer {port.output_layer}, which none of the "
                    f"machine's hatch slots records (layers: {sorted(layers)})"
                )
            if (
                layers
                and port.output_layer is None
                and port.commodity is Commodity.FLUID
                and port.direction is IODirection.OUTPUT
            ):
                raise ValueError(
                    f"fluid output port {port.id!r} names no output layer, but the machine fills "
                    f"its outputs by layer (layers: {sorted(layers)})"
                )


class MachineFaceRef(FrozenModel):
    """A net endpoint: a port on a machine. Frozen/hashable so endpoints dedupe cleanly.
    The solver resolves ``port_id`` to a concrete physical face during placement."""

    machine_id: str = Field(min_length=1)
    port_id: str = Field(min_length=1)


class Net(StrictModel):
    """One logical connection to route: a commodity from/to a set of machine ports.

    ``throughput`` is **typed** - mB/t (fluid), items/t (item), or EU/t (power). Its consumer is
    ``system_io``, which reads it to report boundary feed/product rates on the previewer and build
    guide; the Phase 1 router needs only connectivity, not the rate (per-net tier-cap checks are a
    Phase 2 upgrade). Power is a shared-amperage net, so its physical thickness is computed
    downstream, not stored here.
    """

    id: str = Field(min_length=1)
    commodity: Commodity
    fluid_or_item: str | None = None  # which fluid/item; None for power and for a merged run
    #: The items a **merged run** carries (InputIR v4): one pipe taking every item a single
    #: block ejects through one face to the Item Filters that sort them (#249), or a **feed run**
    #: taking a single block's item inputs from their producers into its one input face (#277),
    #: which reaches no filter. An item net names what it carries in exactly one of the two
    #: fields, ``fluid_or_item`` for one item and this for a merged run, and never both; a fluid or
    #: power net never names ``items`` (GT has no fluid filter block). Read what any net carries
    #: through :attr:`resources`.
    items: tuple[str, ...] = ()
    throughput: float = Field(ge=0.0)
    endpoints: list[MachineFaceRef] = Field(min_length=1)
    #: The ME network this net rides instead of a pipe (InputIR v8, #332): the id of one of
    #: ``InputIR.me.networks``, or ``None`` for a net built physically. The user chooses it per net
    #: (``gtnh-solve --list-nets`` then ``--me-plan``). An item or fluid net only: power never rides
    #: ME (``MEConfig.power_external`` leaves a line's EU supply to the builder instead).
    me_network: str | None = Field(default=None, min_length=1)

    @property
    def rides_me(self) -> bool:
        """Whether this net rides an ME network rather than a pipe: the ONE reading of the choice,
        which every stage that would lay, dock, count or check a pipe for the net asks first."""
        return self.me_network is not None

    @property
    def resources(self) -> tuple[str, ...]:
        """Every fluid or item this net carries: its ``items``, else its one ``fluid_or_item``,
        else nothing (a power net)."""
        if self.items:
            return self.items
        return (self.fluid_or_item,) if self.fluid_or_item else ()

    @model_validator(mode="after")
    def _check(self) -> Net:
        if self.commodity is Commodity.POWER:
            if self.fluid_or_item is not None or self.items:
                raise ValueError("power nets must not name a fluid_or_item or items")
            if self.me_network is not None:
                raise ValueError("a power net never rides ME; leave the supply external instead")
        elif self.items:
            if self.commodity is not Commodity.ITEM:
                raise ValueError(
                    f"only an item net may carry items; this is {self.commodity.value}"
                )
            if self.fluid_or_item is not None:
                raise ValueError("an item net names fluid_or_item or items, not both")
            if any(not item for item in self.items):
                raise ValueError("a merged run's items must each name something")
            if len(self.items) != len(set(self.items)):
                raise ValueError("a merged run's items must not repeat")
        elif not self.fluid_or_item:
            raise ValueError(f"{self.commodity.value} net must name a fluid_or_item")
        return self


class PinnedIO(StrictModel):
    """A fixed external input/output point (e.g. a feed/drain chest) at a cell, tied to
    a net. Honoring it is a hard geometric constraint, checked by the validator."""

    net_id: str = Field(min_length=1)
    cell: CellCoord
    kind: IODirection


class InputIR(StrictModel):
    """The whole problem: machines, nets, fixed/blocked cells, its ME networks, and the
    bounding region the layout must fit. Referential integrity is enforced on build."""

    version: int = INPUT_IR_VERSION
    bounding_region: CellBox
    machines: list[Machine] = Field(default_factory=list)
    nets: list[Net] = Field(default_factory=list)
    pinned: list[PinnedIO] = Field(default_factory=list)
    reserved_cells: list[CellCoord] = Field(default_factory=list)
    #: The ME (AE2) networks the problem's nets may ride, and whether its power is left to the
    #: builder (InputIR v8, #332). Which net rides which is ``Net.me_network``.
    me: MEConfig = Field(default_factory=MEConfig)
    #: Display names for the fluids and items this problem moves, raw id -> name ("liquid_toluene"
    #: -> "Toluene"), as the plan's exporter read them from the game (#296). Display only: every
    #: other field keys a resource by its raw id, and nothing may join on a name. A resource with
    #: no entry is shown by its id. Empty for a problem built from a plan that names nothing.
    resource_names: dict[str, str] = Field(default_factory=dict)
    #: The colour the plan's exporter gives each fluid and item this problem moves, raw id ->
    #: ``"#rrggbb"`` (factory-flow's ``dominantColor``, the average of its in-game icon), lowercased
    #: on the way in (#297). Display only, like :attr:`resource_names`: the previewer draws it as a
    #: swatch where it has no icon to show. Anything but six hex digits is refused, so a value can
    #: go into a stylesheet as it is.
    resource_colors: dict[str, str] = Field(default_factory=dict)
    #: The GTNH pack the plan was balanced against ("2.9.0-beta-2"), or ``None`` when the plan does
    #: not say one or names two. GT's defaults differ between packs in ways a build has to know
    #: (``output_faces.output_side_takes_input``, #278). It changes no geometry, and nothing may
    #: read ``None`` as a particular pack.
    pack_version: str | None = None

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: int) -> int:
        return check_contract_version(value, INPUT_IR_VERSION, "InputIR")

    def _check_me_endpoints(self, networks: set[str]) -> None:
        """Every ME endpoint and infrastructure machine is on one of the problem's networks, and
        an endpoint serves only ports whose nets ride that network: a device on one network cannot
        move a net another carries, nor one that is piped."""
        riding: dict[tuple[str, str], set[str | None]] = {}
        for net in self.nets:
            for ep in net.endpoints:
                riding.setdefault((ep.machine_id, ep.port_id), set()).add(net.me_network)
        for machine in self.machines:
            if machine.me_network is not None and machine.me_network not in networks:
                raise ValueError(
                    f"ME {machine.me_role.value if machine.me_role else 'machine'} "
                    f"{machine.id!r} is on unknown ME network {machine.me_network!r}"
                )
            for endpoint in machine.me_endpoints:
                if endpoint.network not in networks:
                    raise ValueError(
                        f"ME endpoint {endpoint.id!r} of {machine.id!r} is on unknown ME network "
                        f"{endpoint.network!r}"
                    )
                for port in endpoint.ports:
                    on = riding.get((machine.id, port), set())
                    if on != {endpoint.network}:
                        raise ValueError(
                            f"ME endpoint {endpoint.id!r} of {machine.id!r} serves port {port!r}, "
                            f"whose nets ride {sorted(str(n) for n in on) or 'nothing'}, not ME "
                            f"network {endpoint.network!r} alone"
                        )

    def rides_me(self, net: Net) -> bool:
        """Whether ``net`` is left to ME rather than built: an item or fluid net on one of
        :attr:`me`'s networks (``Net.rides_me``), or a power net while the problem's power is the
        builder's (``MEConfig.power_external``). The ONE reading every stage asks before it would
        lay, dock, count or check a connection for a net."""
        if net.commodity is Commodity.POWER:
            return self.me.power_external
        return net.rides_me

    @field_validator("resource_colors")
    @classmethod
    def _check_resource_colors(cls, value: dict[str, str]) -> dict[str, str]:
        colors = {resource: color.lower() for resource, color in value.items()}
        for resource, color in colors.items():
            if not _HEX_COLOR.fullmatch(color):
                raise ValueError(f"resource {resource!r} has colour {color!r}, not '#rrggbb'")
        return colors

    @model_validator(mode="after")
    def _check_referential_integrity(self) -> InputIR:
        machine_ids = [m.id for m in self.machines]
        if len(machine_ids) != len(set(machine_ids)):
            raise ValueError("duplicate machine id")
        net_ids = [n.id for n in self.nets]
        if len(net_ids) != len(set(net_ids)):
            raise ValueError("duplicate net id")

        # port_id -> commodity, per machine, for endpoint resolution + commodity match.
        ports_by_machine = {m.id: {p.id: p.commodity for p in m.faces.ports} for m in self.machines}
        for net in self.nets:
            for ep in net.endpoints:
                machine_ports = ports_by_machine.get(ep.machine_id)
                if machine_ports is None:
                    raise ValueError(f"net {net.id!r} references unknown machine {ep.machine_id!r}")
                if ep.port_id not in machine_ports:
                    raise ValueError(
                        f"net {net.id!r} references unknown port {ep.port_id!r} "
                        f"on machine {ep.machine_id!r}"
                    )
                if machine_ports[ep.port_id] is not net.commodity:
                    raise ValueError(
                        f"net {net.id!r} ({net.commodity.value}) connects to port "
                        f"{ep.port_id!r} of a different commodity"
                    )

        networks = {n.id for n in self.me.networks}
        for net in self.nets:
            if net.me_network is not None and net.me_network not in networks:
                raise ValueError(f"net {net.id!r} rides unknown ME network {net.me_network!r}")
        self._check_me_endpoints(networks)

        net_id_set = set(net_ids)
        for pin in self.pinned:
            if pin.net_id not in net_id_set:
                raise ValueError(f"pinned I/O references unknown net {pin.net_id!r}")
        return self
