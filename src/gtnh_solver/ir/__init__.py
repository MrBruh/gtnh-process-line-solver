"""ir - the two versioned data contracts everything couples to.

``InputIR`` (the problem) and ``LayoutResult`` (the solution, consumed by the previewer and the
``.schematic`` export, and published as JSON on stdout by ``gtnh-solve``). Full spec: docs/IR.md.
Implemented as Pydantic v2 models, split across submodules. The models + versions are re-exported
here as the package's public surface, but the low-level cell-grid *helpers* in ``geometry``
(``FACE_DELTAS``, ``occupied_cells``, ``in_region``, ...) are **not** - consumers deep-import those
from ``ir.geometry`` directly, a convention applied consistently across the placement, router, and
validator lanes. Only the value types (``CellCoord``, ``CellBox``) surface here. The submodules:

- ``enums``      - Commodity, IODirection, Facing, RelativeFace, LayoutStatus, PipeFamily,
                   PipeSize
- ``geometry``   - CellCoord, CellBox (integer cell-grid value types)
- ``input_ir``   - Port, FaceSpec, HatchSlot, StructureBlock, Machine, MachineFaceRef, Net,
                   PinnedIO, InputIR  (+ INPUT_IR_VERSION)
- ``me``         - the ME (AE2) vocabulary (AEColor, MECableKind, MEDeviceKind, MEHatchPolicy,
                   MECards), a problem's ME networks (MEMode, MEStorage, MEPower, MENetworkSpec,
                   MEConfig), and the two contracts that choose them per net (NetKind, NetEnd,
                   NetEntry, NetList, MEPlan; + NETLIST_VERSION, ME_PLAN_VERSION); what a problem
                   asks the ME side for (MERole, MEDeviceSpec, MEEndpoint) and what a layout
                   builds (MECableCell, MEPlacedDevice, MENetworkLayout) and reports per network
                   (MEFlowMetrics, MENetworkMetrics)
- ``nets``       - net helpers shared by the router and the system-IO summary
- ``output``     - Placement, PlacedHatch, Segment, Terminal, Route, RouteMaterial,
                   LayoutMetrics, Infeasibility, LayoutResult  (+ LAYOUT_RESULT_VERSION)

Both roots carry an int ``version``. Additive fields can land without a bump; any change
that breaks an existing consumer bumps the relevant ``*_VERSION`` and updates all
consumers in the same PR. Keep the changelog at the bottom of this file current.
"""

from __future__ import annotations

from .enums import (
    Commodity,
    Facing,
    IODirection,
    LayoutStatus,
    PipeFamily,
    PipeSize,
    RelativeFace,
)
from .geometry import CellBox, CellCoord
from .input_ir import (
    INPUT_IR_VERSION,
    FaceSpec,
    HatchSlot,
    InputIR,
    Machine,
    MachineFaceRef,
    Net,
    PinnedIO,
    Port,
    StructureBlock,
)
from .me import (
    ME_PLAN_VERSION,
    NETLIST_VERSION,
    AEColor,
    MECableCell,
    MECableKind,
    MECards,
    MEConfig,
    MEDeviceKind,
    MEDeviceSpec,
    MEEndpoint,
    MEFlowMetrics,
    MEHatchPolicy,
    MEMode,
    MENetworkLayout,
    MENetworkMetrics,
    MENetworkSpec,
    MEPlacedDevice,
    MEPlan,
    MEPower,
    MERole,
    MEStorage,
    NetEnd,
    NetEntry,
    NetKind,
    NetList,
)
from .output import (
    LAYOUT_RESULT_VERSION,
    AutoConnection,
    Infeasibility,
    LayoutMetrics,
    LayoutResult,
    PlacedHatch,
    Placement,
    Route,
    RouteMaterial,
    Segment,
    Terminal,
)

__all__ = [  # noqa: RUF022 - grouped by section (mirrors definition order), not alphabetized
    # versions
    "INPUT_IR_VERSION",
    "LAYOUT_RESULT_VERSION",
    # enums
    "Commodity",
    "IODirection",
    "Facing",
    "RelativeFace",
    "LayoutStatus",
    "PipeFamily",
    "PipeSize",
    # geometry
    "CellCoord",
    "CellBox",
    # input IR
    "Port",
    "FaceSpec",
    "HatchSlot",
    "StructureBlock",
    "Machine",
    "MachineFaceRef",
    "Net",
    "PinnedIO",
    "InputIR",
    # ME (AE2): vocabulary, a problem's networks, and the per-net choice
    "AEColor",
    "MECableKind",
    "MEDeviceKind",
    "MEHatchPolicy",
    "MECards",
    "MEMode",
    "MEStorage",
    "MEPower",
    "MENetworkSpec",
    "MEConfig",
    "NETLIST_VERSION",
    "ME_PLAN_VERSION",
    "NetKind",
    "NetEnd",
    "NetEntry",
    "NetList",
    "MEPlan",
    "MERole",
    "MEDeviceSpec",
    "MEEndpoint",
    "MECableCell",
    "MEPlacedDevice",
    "MENetworkLayout",
    "MEFlowMetrics",
    "MENetworkMetrics",
    # output schema
    "Placement",
    "PlacedHatch",
    "Segment",
    "Terminal",
    "Route",
    "RouteMaterial",
    "AutoConnection",
    "LayoutMetrics",
    "Infeasibility",
    "LayoutResult",
]


# LayoutResult v1 (BREAKING) - added `LayoutResult.hatches: list[PlacedHatch]`, one record per
#   hatch or bus the build needs, routed and upkeep alike, at the body cell it occupies and the way
#   it faces. Additive in shape and still a bump, because the omission is what breaks: a maintenance
#   hatch and a muffler belong to no net and had nowhere to live, so a v0 layout described a machine
#   that will not run, and a consumer that ignores the new field keeps describing one. A routed
#   hatch is deliberately recorded twice, here and by its `Terminal`; the validator re-derives the
#   agreement rather than trusting it (docs/ARCHITECTURE.md #4).
#
# InputIR v3 (additive, no version bump) - added `Machine.hatch_slots: tuple[HatchSlot, ...]`, where
#   a multiblock's hatch-capable casing cells actually are, as offsets from its unrotated minimum
#   corner plus the kinds each accepts. No bump because it is purely additive and an empty tuple
#   reads as "unknown" exactly the way the old behaviour did: a machine without it stays dockable on
#   any body face. `kinds` is a LOWER bound, never a whitelist - see `HatchSlot`.
# ---------------------------------------------------------------------------
# Contract changelog (bump the relevant *_VERSION on any breaking change):
#
# InputIR v0 / LayoutResult v0 - initial implementation of the docs/IR.md draft.
#   Concretizations made where the doc left shapes open (reconciled into docs/IR.md):
#   - FaceSpec is a list of `Port` (id/commodity/direction/cover); the physical face is a
#     solver decision, so FaceSpec is a port catalog, not a face map.
#   - MachineFaceRef references a machine + port_id (resolved to a face by the solver).
#   - Geometry `Box`/`CellBox` unified into one `CellBox` (a size, each dim >= 1).
#   - Segment fields named `start`/`end` (doc's `from` is a Python keyword).
#
# LayoutResult v0 (additive, no version bump) - added `Route.terminals: list[Terminal]`
#   (machine_id/port_id/face/cell) so a route records where it docks on each machine
#   endpoint. Existing consumers default to an empty list; the router fills it and the
#   validator checks face/adjacency/on-route reachability.
#
# LayoutResult v0 (additive, no version bump) - added `LayoutResult.auto_connections:
#   list[AutoConnection]`. A net is satisfied by EITHER a pipe `Route` OR an
#   `AutoConnection` (adjacent machines auto-feeding, no pipe). Machine `orientation` is
#   horizontal-only (GT machines never face up/down).
#
# InputIR v1 (BREAKING) - dropped `Machine.count`. Multi-instance machine groups are not
#   supported until instance-aware routing exists (Phase 2): the placer expanded `count` into
#   N placements sharing one machine id, but the router/solver/validator collapsed them via
#   `setdefault` and a `MachineFaceRef` cannot address a specific instance - counted machines
#   were placed yet silently left unwired. Each `Machine` is now exactly one instance; the
#   adapter rejects an export `machineCount > 1` with an explicit `AdapterError`. `count`
#   returns once routing is instance-aware.
#
# InputIR v1 (additive, no version bump) - added `Machine.eut: float` (EU/t draw). With
#   `voltage_tier` it gives the amperage a machine pulls on a shared-amperage cable
#   (dataset.amperage); the adapter sets it from the recipe and synthesizes a power source +
#   net per voltage tier. 0 for unpowered blocks / sources. Existing consumers default to 0.
#
# InputIR v2 (BREAKING) - dropped `Port.is_auto_output` (and its FaceSpec validation). It was a
#   dead, contradictory field: the adapter never set it and the solver auto-connects any adjacent
#   output regardless. Whether a port is satisfied by auto-output is a SOLVER DECISION, not a
#   problem input - it lives in the output's `AutoConnection`, and the "one auto-output per
#   machine, items-xor-fluids, never power" rule is enforced there by the validator
#   (DUPLICATE_AUTO_OUTPUT / AUTO_OUTPUT_ILLEGAL_COMMODITY), not on the input contract.
#
# InputIR v2 (additive, no version bump) - added `Port.rate: float | None` (items/t or mB/t moved
#   through the port; None for power or when unknown). The adapter computes it from the recipe;
#   `system_io` + the previewer use it to surface boundary input/output rates - notably a dangling
#   OUTPUT port, whose product rate lives nowhere else (no consuming net). Existing consumers
#   default to None. (GitHub #16.)
#
# InputIR v2 (additive, no version bump) - added `Machine.block_key: str | None`, the GT controller
#   block as "<registry_name>@<meta>". `Machine.type` is the exporter's localized recipe-map name,
#   which for GT++ machines is NOT the controller block's name the structure dataset is keyed by
#   ("Chemical Plant" vs "ExxonMobil Chemical Plant"), so name-only lookups silently missed and
#   those machines fell back to a 1x1x1 footprint. The adapter sets it from the export's
#   `recipe.source.machineBlock` (gtnh-factory-flow #25); consumers that have it join exactly and
#   fall back to `type` when it is None, so pre-#25 plans behave exactly as before. (GitHub #98.)
#
# InputIR v3 (BREAKING) - a power port's `rate` is now meaningful: it is the EU/t arriving through
#   THAT connection, and a machine's power input ports must sum to its `eut`. Previously a power
#   port carried no rate and every consumer charged a cable the whole machine's draw, which is only
#   right while a machine has one power connection. A GT multiblock does not: an energy hatch
#   accepts 2 A (`MTEHatchEnergy.maxAmperesIn`), so a machine drawing more than that spreads its
#   intake over several hatches, and each cable carries only its own hatch's share. Breaking
#   because a producer that emits several power ports and a consumer that still reads `Machine.eut`
#   per terminal disagree by a multiple - hence the bump, even though the field itself is old.
#   Read a port's share through `Machine.port_eut`, which falls back to `eut` for an unrated port
#   so single-connection machines are unchanged.
#
# InputIR v3 (additive, no version bump) - added `Port.max_amps: float | None`, the most amps one
#   connection accepts (2 for an energy hatch), and `Machine.hatch_cells: int | None`, how many
#   structure cells can host a hatch at all. A multiblock's casing cells take I/O of any kind,
#   power included, so these two say how far a draw may be split and how many connections the
#   structure can physically host. Both default to None ("no ceiling known"), so a problem built
#   without the physical dataset behaves exactly as before.
#
# InputIR v3 (clarification, no version bump) - `Port.max_amps = null` is settled as "the ceiling
#   is UNKNOWN", never "unlimited". docs/IR.md used to say the latter while the validator had
#   always implemented the former; both verdicts happened to coincide, so nothing was live, but a
#   versioned contract should not carry two readings. Every GT connection has a ceiling - the
#   producer either names the rule (2 A for an energy hatch, `maxAmperesIn` for a machine known to
#   be a single block) or abstains - so a consumer must treat null as unmeasurable rather than
#   satisfied. No producer or consumer changed; only the wording, in the same PR as the validator
#   change that made the distinction observable (#114).
#
# LayoutResult v1 (additive, no version bump) - added `Route.material: RouteMaterial | None`, the
#   tier-representative cable or pipe a route is drawn and costed as. It exists so the build guide's
#   bill of materials and the previewer cannot disagree about what a route is made of: both read it
#   rather than each deriving a guess. `None` means "unspecified pipe", which is exactly what every
#   route said before the field existed, so a consumer that ignores it renders precisely today's
#   build - correct, just less specific. That is the test the additive rule asks for, and it is why
#   this does not bump where `hatches` did: a consumer ignoring `hatches` describes a structure that
#   will not form, which is a different kind of wrong.
#
#   `stand_in` is True on every material v1 emits, enforced by the model. GT has many cable
#   materials per tier (six at LV), the solver has never chosen between them, and inventing one is
#   the failure docs/dataset-extraction/texture-resolution.md calls unrecoverable - so the flag
#   travels on the contract for the sake of the consumer that must refuse it: `.schematic` export
#   (#4, #96) may not lower a stand-in into a real block.
#
# Both roots (enforcement, no version bump) - `version` is now CHECKED on parse: a payload whose
#   version is not this build's is rejected with a pointed error instead of validating clean.
#   Until now the field enforced nothing - `InputIR.model_validate({"version": 0, ...})` returned a
#   model reporting v0 against a v3 contract - so the one field whose whole job is to catch a
#   contract mismatch was the one field that could not. `extra="forbid"` covers the other half
#   (fields this build does not know) but cannot see a bump that changed what an existing field
#   MEANS, which is the v2 -> v3 power-rate change exactly: same shape, different meaning, silently
#   read as if it agreed. A *newer* version is refused as firmly as an older one, since being
#   unable to name what changed is the reason to refuse rather than a reason to hope.
#
#   No bump: this tightens what the contract ACCEPTS, it does not change what it says. A producer
#   emitting the current version is unaffected, and the only payloads that now fail are ones that
#   were already being misread. (GitHub #38.)
#
# InputIR v3 (no change to the contract) - multi-instance nodes are supported, and `Machine.count`
#   is NOT coming back. The v1 entry above expected it to "return once routing is instance-aware";
#   it turned out nothing had to return. `Net.endpoints` is already an unbounded list, so a node
#   standing for N machines maps to N `Machine`s sharing one net: the router chains the endpoints
#   and the validator already permits several producers, which is also what a real GT line is (one
#   shared bus, not per-instance pipes). The v1 failure was never the missing field - it was that
#   the placer expanded `count` into placements sharing ONE machine id, so a `MachineFaceRef` could
#   not name an instance. Distinct ids fix that without a contract concept.
#
#   Nothing here changed, so nothing is bumped; the note exists because the v1 entry above states a
#   plan this supersedes. Consumers should know only that `Machine.id` may now carry a `#N` suffix,
#   and that a node with one machine still uses its bare id. (GitHub #76.)
#
# LayoutResult v2 (BREAKING) - added `RouteMaterial.size: PipeSize | None`, the gauge a fluid or
#   item pipe is built at (tiny, small, normal, large, huge). Required on a pipe and invalid on a
#   cable, the mirror of `tier`; a cable's gauge stays per segment in `thickness_per_segment`.
#   Additive in shape and still a bump, for the reason `hatches` was: the omission is what breaks.
#   Every v1 pipe was the normal size, and the maintainer's in-game build of the parallel sand line
#   proved that a normal tin pipe feeding three hammers from one chest fed one of them, so a
#   consumer that ignores the field builds a line that runs at a third of its rate with nothing
#   raising. The size is real rather than a stand-in, like a cable's thickness.
#
#   Not readable from v1, by decision rather than by accident. The one field is new and a v1 pipe
#   was always "normal", so an upgrade-on-read would be easy to write, but the contract refuses any
#   payload whose version is not its own (the #38 entry above), and an exception carved out for one
#   bump is a rule nobody can rely on. A v1 layout is regenerated by re-solving its plan. Nor does
#   `size` default to "normal" within v2: a pipe that silently falls back to the normal size is the
#   exact failure this bump ends, so a producer must name the size it chose. (GitHub #165.)
#
# InputIR v3 (clarification, no version bump) - `Machine.block_key` names the controller the adapter
#   RESOLVED, not only one the export supplied. The adapter used to stamp the export's
#   `machineBlock` id and nothing else, so a machine it joined to the structure dump by handler
#   label, recipe-map name or alias (every arodoid / GTNH 2.9 plan, which carries no
#   `machineBlock`) arrived keyless, and the previewer and `.schematic` exporter fell back to
#   `type`: a recipe-map name 2.9 does not use as a controller name, and one that can name a
#   DIFFERENT controller. A Dangote Distillus (recipe map "Distillation Tower") drew and exported as
#   a plain Distillation Tower, mID 1126 instead of 31021, with no error. The adapter now stamps the
#   key of the record it resolved, by block id, handler label, recipe-map name or alias, so the
#   drawn structure is always the one the footprint and hatch slots were reserved from; an export
#   key the dump does not know gives way to the record a name resolved. Without a resolved record
#   (no dataset, or a dump miss) the export's own key still passes through, so null now means both
#   that no record was resolved and that the export named none. The v2 entry's "pre-#25 plans
#   behave exactly as before" is deliberately no longer true: a pre-#25 plan that resolves by name
#   now carries a key and draws exactly what its footprint was reserved from. Same field and type,
#   and non-null still means an exact controller identity, so no bump. Only the previewer and the
#   exporter read the field; nothing in the router, solver or validator does, so no `LayoutResult`
#   can change. (GitHub #205.)
#
# LayoutResult v2 (no change to the contract) - `gtnh-solve` now PUBLISHES it: with no `--preview`
#   and no `--schematic`, the CLI prints the layout as JSON on stdout, infeasible runs included,
#   where it used to print the text build guide (now retired). Nothing here changed, so nothing is
#   bumped; what is new is that a script can consume the contract directly, which makes the
#   versioning rules above bind on a consumer outside this repo too. The JSON uses field names (the
#   contract has no aliases), states every field, and escapes non-ASCII. (GitHub #203.)
#
# LayoutResult v3 (BREAKING) - a pipe route may be ONE block: a route with no `segments` whose
#   terminals all share one cell is a single pipe block wired straight to each of their machines.
#   Until now every route needed a segment (ROUTE_DISCONTINUOUS), so when both ends of a pipe net
#   docked on one cell the router laid a second block beside it just to have a hop, and that block
#   led nowhere: a dead-end pipe in the nitrobenzene preview, on the nitrogen and water feeds. A
#   cable still needs a segment, because its gauge lives in `thickness_per_segment`.
#
#   The shape is unchanged (`segments` was always allowed to be empty), and yet a bump, for the
#   omission rule `hatches` set: a v2 consumer that builds a route's blocks from its segments builds
#   nothing for a one-block pipe, and the line is missing a connection with nothing raising.
#   `Route.cells()` is the one reading of a route's blocks that covers both forms, so a consumer
#   should use it (or, like `route_blocks`, take the terminals' cells as well as the segments').
#
# InputIR v3 (additive, no version bump) - added `Machine.recipe_map: str | None`, the unlocalized id
#   of the GT recipe map the machine runs ("gt.recipe.orewasher"), read off the export's
#   `recipe.source.rawRecipeId`. `Machine.type` is that map's localized name, which a single-block
#   machine's own name often does not contain ("Ore Washer" vs "Basic Ore Washing Plant") and which
#   two maps can share ("Furnace" for both the furnace and the microwave), so the previewer and the
#   exporter drew some machines as the wrong block, or as none. They now join on this and the
#   voltage tier first. None for storages, power sources and a plan that does not state it, where
#   they fall back to `type` exactly as before. (GitHub #232.)
#
# InputIR v4 (BREAKING) - a port may be PINNED to some of its machine's faces, and an item net may
#   carry several items. Three fields, all for GitHub #249: a single block has five faces that can
#   carry a connection, and a machine with several item outputs spent one on each, so an Ore Washer
#   (item in, fluid in, three item outputs, power) could not be built. GT sends such a machine's
#   items out of one face into one pipe and sorts them with Item Filter blocks, and the adapter now
#   places those filters as ordinary machines.
#   - `Port.faces: tuple[RelativeFace, ...] | None`, the faces a port may dock on, named from the
#     machine's point of view (front, back, left, right, up, down) so they turn with it. None is
#     the old rule, any face but the front. An Item Filter pushes out of its back and nowhere else,
#     and takes items on every other face, its front included. Read through
#     `Machine.allowed_faces(port_id, orientation)`, the one reading of the face rule every stage
#     now shares.
#   - `Net.items: tuple[str, ...]`, the items a merged run carries. An item net names what it
#     carries in `fluid_or_item` (one item) or `items` (a merged run), exactly one; fluid and power
#     nets never name `items`. `Net.resources` reads either.
#   - `Machine.filter_items: tuple[str, ...]`, what an Item Filter lets through; empty elsewhere.
#   Breaking by the rule `hatches` set: the omission is what breaks. A v3 consumer that ignores
#   `Port.faces` docks a filter's output on a side face, where the block never pushes anything, and
#   one that reads only `fluid_or_item` finds a merged run naming nothing. Existing machines and
#   nets are unchanged in shape (every new field defaults to the old meaning), but a v3 payload is
#   still refused on parse, per the #38 rule above; re-adapt the plan.
#
# InputIR v5 (BREAKING) - a machine may stand for something built OUTSIDE the layout. One field,
#   for GitHub #282: a crop card is an input to the line, like the power source, so the adapter
#   places one Crop Manager whose output feeds the line and whose field is not part of the build.
#   - `Machine.outside_front: bool`, whether its front faces outside the build. Like a power
#     source's feed face, that front lies flush on the region boundary; placement keeps it there
#     and the validator holds it there. Read through `Machine.fronts_outside`, which also covers a
#     power source (known by its power output port, so it needs no flag).
#   Breaking by the same rule: a v4 consumer that ignores the flag places a Crop Manager anywhere,
#   its field face buried among the line's machines. A v4 payload is refused on parse.
#
# InputIR v5 (additive, no version bump) - added `InputIR.resource_names: dict[str, str]`, the
#   display name of each fluid and item the problem moves (raw id -> "Toluene"), as the plan's
#   exporter read it from the game (#296). Display only: every other field still keys a resource by
#   its raw id, and nothing joins on a name. Not breaking by omission either: a consumer that
#   ignores it shows bare ids, which is what every consumer did before, and builds nothing wrong.
#
# InputIR v5 (additive, no version bump) - added `InputIR.pack_version: str | None`, the GTNH pack
#   the plan was balanced against (the adapter's `plan_pack_version`), for GT defaults that differ
#   between packs (#278: a 2.9 basic machine takes input through its output face, a 2.8.4 one does
#   not). It changes no geometry; a consumer that ignores it only loses pack-specific build advice.
#
# LayoutResult v3 (additive, no version bump) - added `LayoutMetrics.rounds: int | None`, how many
#   rounds of the multi-start a solve ran. Set only when the solve was given a time budget or a
#   round count (`gtnh-solve --time-budget` / `--rounds`), and left out of the dump while None, so a
#   layout solved without either serializes byte for byte as before. Metrics are advisory and
#   `extra="allow"`, so a consumer that does not know the field loses nothing it needs to build;
#   what it carries is how to reproduce a timed solve (`--rounds` that many, same seed).
#
# LayoutResult v4 (BREAKING) - a net may be PARTLY auto-connected. On a net with several producers
#   and one consumer, each producer standing against the consumer ejects into it by auto-output (an
#   `AutoConnection` of its own), and the net's `Route` carries terminals for the other producers
#   and the consumer only; a net whose every producer is covered has no route at all. Until now
#   each net was satisfied by exactly one of a route or an auto-connection, so three Forge Hammers
#   feeding one Super Chest were always piped, even with a hammer standing against the chest.
#   (GitHub #270.)
#
#   The shape is unchanged, and yet a bump, because what an existing field means changed: a v3
#   consumer may read a net with an auto-connection as having no pipe and skip its route, or read
#   a route as reaching every endpoint and look for a terminal on a producer that has none. Either
#   way the build it describes is missing a connection, with nothing raising. A v3 layout is
#   refused on parse, as every bump is; re-solve its plan.
#
# InputIR v6 (BREAKING) - a tower's fluid outputs are tied to their layers. GT fills a Distillation
#   Tower by layer, not first fit: recipe fluid output `i` goes only to the output hatches on the
#   tower's `i`-th layer above the base, and the tower does not form while any layer has none
#   (`MTEDistillationTower.addFluidOutputs`, `checkMachine`). Two fields, for GitHub #299:
#   - `HatchSlot.output_layer: int | None`, the per-layer output list GT files a hatch in that
#     cell under, from the dump (dataset schema v3). `Machine.output_layers` collects them.
#   - `Port.output_layer: int | None`, the layer a tower's fluid output port is filled from: the
#     fluid's place among the recipe's fluid outputs. Only a fluid output may name one, it must be
#     a layer the slots record, and on a machine whose slots record layers every fluid output names
#     one. `Machine.hatch_slots_for` then gives such a port exactly its kind's slots on that layer,
#     with no fallback.
#   Breaking by omission, the rule `hatches` set: a v5 consumer that ignores the fields docks a
#   tower's output hatches on any layer, so each product leaves through whichever hatch GT fills
#   with another, and a layer left without a hatch keeps the tower from forming. A v5 payload is
#   refused on parse; re-adapt the plan.
#
# LayoutResult v4 (additive, no version bump) - a `PlacedHatch` with `port_id = None` is an upkeep
#   hatch (maintenance, muffler) OR a spare output hatch: GT forms a tower only with an output
#   hatch on every layer, so a layer no product uses gets one that receives nothing (#299). The
#   shape is unchanged and every such hatch is a real block to place; a consumer that reads
#   `port_id = None` as "upkeep" only mislabels the spare, and builds the same structure.
#
# InputIR v6 (additive, no version bump) - added `InputIR.resource_colors: dict[str, str]`, the
#   plan's colour for each fluid and item the problem moves (raw id -> "#rrggbb", the exporter's
#   `dominantColor`), which the previewer draws as a swatch where it has no icon (#297). Display
#   only, like `resource_names`. Values are lowercased and anything but `#` and six hex digits is
#   refused on parse, so a consumer may put one into a stylesheet as it is.
#
# InputIR v7 (BREAKING) - added `Machine.structure_blocks: dict[str, StructureBlock]`, the block each
#   tiered part of a multiblock is built from, keyed by GT's channel id (#312). `StructureBlock` is
#   `{block, meta}`. The adapter fills the node's coil on any multiblock with a `coil` channel, and
#   on an ExxonMobil Chemical Plant its solid casing (the cheapest whose tier meets the recipes'
#   special value, `validateRecipe`), its pipe casing (the plan's, which sets its parallels) and its
#   machine casing (the tier it is supplied at, so every hatch it gets forms, `checkMachine`). A
#   channel the field does not name is built as the dump draws it.
#   Breaking by omission, the rule `hatches` set: a v6 consumer that ignores the field draws and
#   exports the dump's default build, Bronze solid casing on LV machine casings, where GT refuses
#   the nitrobenzene recipe and HV hatches keep the plant from forming at all. A v6 payload is
#   refused on parse; re-adapt the plan.
#
# Both roots (additive, no version bump) - added the `ir.me` module, the ME (AE2) vocabulary:
#   `AEColor`, `MECableKind`, `MEDeviceKind`, `MEHatchPolicy` and `MECards` (#331). Value types
#   only: neither root carries one yet, so no payload changes and nothing is bumped. The contracts
#   that use them (the ME networks a net rides, the ME devices a layout places) bump their roots
#   when they land.
#
# InputIR v8 (BREAKING) - ME is chosen PER NET, not per commodity (#332). `METoggles` and
#   `InputIR.me_toggles` are gone; in their place:
#   - `InputIR.me: MEConfig`, the ME networks the problem's nets may ride (`MENetworkSpec`: id,
#     attached or subnet, storage, power, hatch policy, colour, channel budget, Super Speed) and
#     `power_external`, which is the old power toggle: the line's EU supply is the builder's;
#   - `Net.me_network: str | None`, the network a net rides, read through `Net.rides_me`. Every
#     stage that skipped a toggled commodity now skips a net that rides ME.
#   A user makes the choice against two new contracts, versioned on their own: `NetList` (what
#   `gtnh-solve --list-nets` prints) and `MEPlan` (what `--me-plan` reads), both in `ir.me`.
#   Breaking by removal: a v7 payload names `me_toggles`, which this build refuses, and a v7
#   consumer reading it finds nothing. Re-adapt the plan. ME nets are still only SKIPPED, as toggled
#   commodities were: nothing places an ME device for them until the end-to-end build (#335).
#
# InputIR v9 (BREAKING) - a problem says what its ME side must build (#333):
#   - `Machine.me_endpoints: tuple[MEEndpoint, ...]`, one per ME device the machine needs: the
#     ports it serves (on nets riding its network), the device (`MEDeviceSpec`: kind, a GT ME
#     hatch's mID, cards, config), on a multiblock the hatch slot kind it takes, and its share of
#     a port too fast for one device;
#   - `Machine.me_role: MERole | None` and `Machine.me_network`, for an infrastructure block (an
#     attach stub, a link, a controller, an acceptor); a stub and a link face outside the build.
#   Breaking by omission: a v8 consumer that ignores the endpoints builds a line whose ME nets
#   have no device, and one that ignores the roles builds a controller as a machine of the line.
#   The adapter emits neither until the end-to-end build (#335). A v8 payload is refused on parse.
#
# LayoutResult v5 (BREAKING) - added `LayoutResult.me_networks: list[MENetworkLayout]` (#333), each
#   network's cable blocks (`MECableCell`: cell, kind, the channels the solver routed through it)
#   and devices (`MEPlacedDevice`: an AE2 part on a cable side, or a GT ME hatch, with its cards
#   and config). Breaking by omission, the rule `hatches` set. The validator rebuilds which blocks
#   join which network in game from the blocks themselves, and runs AE2's channel pathing on it
#   (`validator.me`). A v4 layout is refused on parse; re-solve its plan.
#
# LayoutResult v5 (additive, no version bump) - added `LayoutMetrics.me: list[MENetworkMetrics]`
#   (#336), what each ME network asks of the player: its mode, colour and power, its devices and
#   channel budget, the main network's channels it spends, its draw in AE/t and EU/t computed from
#   the cable the layout lays (spike 6), the store an external subnet's power must hold for one
#   GT ME output flush, an acceptor's rating, source and the most amps to feed that source (its
#   cable is sized for exactly that), and what its storage supplies and takes in
#   (`MEFlowMetrics`). It is what the site, which reads only the
#   layout, shows per network. Metrics are advisory, and it is left out of the dump while empty, so
#   a layout with no ME network serializes byte for byte as before and a consumer that ignores it
#   builds nothing wrong. An acceptor network's Energy Acceptor is a placement (`Machine.me_role`),
#   so building it needs no new field either.
#
# ---------------------------------------------------------------------------
