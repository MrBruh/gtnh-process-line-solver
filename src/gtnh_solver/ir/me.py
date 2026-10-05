"""The ME (AE2) vocabulary the contracts and the rule data share, and the contracts that choose ME.

Applied Energistics 2 builds a line's ME side from a handful of things this module names: the
cable kinds a network is laid in (:class:`MECableKind`), the seventeen colours that keep two
networks apart (:class:`AEColor`), the devices that move items and fluids between a machine and the
network (:class:`MEDeviceKind`), the upgrade cards a bus is fitted with (:class:`MECards`), and the
policy that decides when a multiblock uses GT's own ME hatches (:class:`MEHatchPolicy`). The RULES
about them, which colours connect, how many channels a cable carries, how fast a bus moves, are DATA
in :mod:`gtnh_solver.dataset.me`, cited against the pack's source in ``docs/spikes/329-me-ae2.md``.

**Which nets ride ME is the user's choice, per net** (#332), made in two steps against two more
versioned contracts::

    gtnh-solve plan.json --list-nets      ->  NetList   every item and fluid net, with its ends,
                                                        its rate and the device each end would get
    (the user, or gtnh-solver-site's picker)
    gtnh-solve plan.json --me-plan FILE   <-  MEPlan    the ME networks, and which net rides which

The adapter stamps the choice onto the problem as :class:`MEConfig` (``InputIR.me``) and
``Net.me_network``, and what each machine needs built as its :class:`MEEndpoint` s and, for an
infrastructure block, its :class:`MERole` (InputIR v9). A layout answers with an
:class:`MENetworkLayout` per network: its cable blocks and its devices (LayoutResult v5). Net ids in both files are the adapter's **pre-merge** ids, deterministic for a
plan and a dataset, which is why both carry the plan's digest and the dataset's identity: a choice
made against another plan or dataset is refused rather than applied to nets it never saw.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field, field_validator, model_validator

from ._base import FrozenModel, StrictModel, check_contract_version
from .enums import Commodity, Facing
from .geometry import CellCoord

#: Bump on any breaking change to the NetList contract (``gtnh-solve --list-nets``).
NETLIST_VERSION = 1
#: Bump on any breaking change to the MEPlan contract (``gtnh-solve --me-plan``).
ME_PLAN_VERSION = 1
#: The free channels an attached network may spend on the player's main network when its plan does
#: not say: one dense cable's worth (spike 1.1).
DEFAULT_ME_CHANNEL_BUDGET = 32


class AEColor(str, Enum):
    """An AE2 cable or device colour, in AE2's own order (``appeng.api.util.AEColor``).

    **Declaration order IS AE2's ordinal**, which is what a cable's item damage adds to its kind's
    base (spike 7.3): use :attr:`ordinal` for the number. ``FLUIX`` is AE2's ``Transparent``, the
    uncoloured cable; which colours connect is rule data (``dataset.me.colours_connect``).
    """

    WHITE = "white"
    ORANGE = "orange"
    MAGENTA = "magenta"
    LIGHT_BLUE = "light_blue"
    YELLOW = "yellow"
    LIME = "lime"
    PINK = "pink"
    GRAY = "gray"
    LIGHT_GRAY = "light_gray"
    CYAN = "cyan"
    PURPLE = "purple"
    BLUE = "blue"
    BROWN = "brown"
    GREEN = "green"
    RED = "red"
    BLACK = "black"
    FLUIX = "fluix"

    @property
    def ordinal(self) -> int:
        """AE2's ordinal for this colour: White 0 through Black 15, Fluix 16."""
        return list(AEColor).index(self)


class MECableKind(str, Enum):
    """An AE2 cable, by what it is built from. Only the smart and dense kinds show their channels.

    How many channels each carries, and which take parts, is rule data
    (``dataset.me.CABLE_CAPACITY``, ``dataset.me.PART_CABLES``).
    """

    GLASS = "glass"
    COVERED = "covered"
    SMART = "smart"
    DENSE = "dense"
    DENSE_COVERED = "dense_covered"


class MEDeviceKind(str, Enum):
    """A device that moves a machine's items or fluids between it and an ME network.

    Two families, by what the device is in the world:

    - **AE2 parts**, which sit on one side of a cable block and work on the block that side faces:
      AE2's item buses and interface, and AE2FluidCraft's fluid buses and Dual Interface. The Dual
      Interface takes items as well as fluids (spike 4.6), so it serves a single block whose items
      and fluids leave through one face;
    - **GT's own ME hatches**, which take a multiblock hatch slot like any other hatch and connect to
      AE through their front face alone (spike 5.2).

    What each moves per tick, and what it costs, is rule data (:mod:`gtnh_solver.dataset.me`).
    """

    IMPORT_BUS = "import_bus"
    EXPORT_BUS = "export_bus"
    STORAGE_BUS = "storage_bus"
    INTERFACE = "interface"
    FLUID_IMPORT_BUS = "fluid_import_bus"
    FLUID_EXPORT_BUS = "fluid_export_bus"
    FLUID_STORAGE_BUS = "fluid_storage_bus"
    DUAL_INTERFACE = "dual_interface"
    GT_OUTPUT_BUS_ME = "gt_output_bus_me"
    GT_OUTPUT_HATCH_ME = "gt_output_hatch_me"
    GT_STOCKING_INPUT_BUS_ME = "gt_stocking_input_bus_me"
    GT_STOCKING_INPUT_HATCH_ME = "gt_stocking_input_hatch_me"


class MEHatchPolicy(str, Enum):
    """When a multiblock's ME connection is one of GT's own ME hatches.

    - ``tier_aware`` (the default): when the line has reached the hatch's tier, so the player can
      make it;
    - ``always``: whatever the line's tier;
    - ``never``: never; the multiblock keeps a normal hatch and an AE2 part in front of it moves its
      contents.
    """

    TIER_AWARE = "tier_aware"
    ALWAYS = "always"
    NEVER = "never"


class MECards(FrozenModel):
    """The upgrade cards fitted to one AE2 bus.

    ``acceleration`` is AE2's Acceleration Card (its ``CardSpeed``), ``super_speed`` GT:NH's
    Hyper-Acceleration Card (``CardSuperSpeed``), ``capacity`` the Capacity Card that opens more
    config slots. AE2 takes at most four of each, which is all this states; that they share a bus's
    four upgrade slots is a rule the validator checks (``dataset.me.UPGRADE_SLOTS``), not a shape.
    """

    acceleration: int = Field(default=0, ge=0, le=4)
    super_speed: int = Field(default=0, ge=0, le=4)
    capacity: int = Field(default=0, ge=0, le=4)

    @property
    def count(self) -> int:
        """How many upgrade slots these cards take."""
        return self.acceleration + self.super_speed + self.capacity


class MEMode(str, Enum):
    """How an ME network relates to the player's own.

    - ``attached``: its devices join the player's main network through one or more dense cable
      stubs on the region's edge, spending that network's free channels (a budget the user states);
    - ``subnet``: an independent network of its own colour, with a controller or ad hoc, reaching
      the player's storage through a link (:class:`MEStorage`).
    """

    ATTACHED = "attached"
    SUBNET = "subnet"


class MEStorage(str, Enum):
    """Where a subnet's items and fluids are kept.

    - ``link`` (the default): in the player's main network, through a storage bus on the subnet
      facing an ME Interface on the main network, which costs the main network one channel;
    - ``chests``: in the line's own boundary Super Chests and Super Tanks, each read by a storage
      bus partitioned to its resource.
    """

    LINK = "link"
    CHESTS = "chests"


class MEPower(str, Enum):
    """How an ME network is powered.

    - ``external`` (the default): the builder supplies it (a quartz fiber, the main network); the
      run reports what it draws;
    - ``acceptor``: an Energy Acceptor on the line's own EU supply.
    """

    EXTERNAL = "external"
    ACCEPTOR = "acceptor"


class MENetworkSpec(FrozenModel):
    """One ME network a line's nets may ride, as the user configured it.

    ``colour`` is the network's cable colour. An attached network is Fluix, the player's own; a
    subnet must be coloured, or AE would join it to the main network wherever the two touch, and
    one left unset gets the first colour no other subnet asked for (:meth:`MEConfig.colour`).
    ``me_channel_budget`` is how many free channels the player's main network has for an attached
    one; a subnet spends one (``link``) or none (``chests``), so it reads no budget.
    ``super_speed`` allows Hyper-Acceleration Cards, which a line still gets only from LuV
    (``dataset.me.super_speed_allowed``).
    """

    id: str = Field(min_length=1)
    mode: MEMode
    storage: MEStorage = MEStorage.LINK
    power: MEPower = MEPower.EXTERNAL
    hatches: MEHatchPolicy = MEHatchPolicy.TIER_AWARE
    colour: AEColor | None = None
    me_channel_budget: int = Field(default=DEFAULT_ME_CHANNEL_BUDGET, ge=1)
    super_speed: bool = True

    @model_validator(mode="after")
    def _check(self) -> MENetworkSpec:
        if self.mode is MEMode.ATTACHED:
            if self.colour is not None and self.colour is not AEColor.FLUIX:
                raise ValueError(
                    f"ME network {self.id!r} is attached to the main network, whose cable is "
                    f"Fluix; it cannot be {self.colour.value}"
                )
            if self.storage is MEStorage.CHESTS:
                raise ValueError(
                    f"ME network {self.id!r} is attached, so it stores in the main network; "
                    f"'chests' is a subnet's storage"
                )
        elif self.colour is AEColor.FLUIX:
            raise ValueError(
                f"ME subnet {self.id!r} cannot be Fluix: Fluix cable joins every colour, so the "
                f"subnet would merge with the main network wherever they touch"
            )
        return self


def _check_networks(networks: list[MENetworkSpec]) -> None:
    """The rules a set of ME networks keeps together: unique ids, at most one attached network (the
    player has one main network), and no two subnets of one colour (they would merge)."""
    ids = [n.id for n in networks]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate ME network id")
    attached = [n.id for n in networks if n.mode is MEMode.ATTACHED]
    if len(attached) > 1:
        raise ValueError(
            f"ME networks {attached} are all attached to the one main network; make them one"
        )
    colours = [n.colour for n in networks if n.mode is MEMode.SUBNET and n.colour is not None]
    if len(colours) != len(set(colours)):
        raise ValueError("two ME subnets share a colour, so they would merge where they touch")


class MEConfig(StrictModel):
    """The ME side of a problem (``InputIR.me``): its networks, and whether power is left to ME.

    Which net rides which network is on the net (``Net.me_network``). ``power_external`` says the
    line's EU supply is the builder's (``gtnh-solve --me power``): no power source is placed and
    no cable is laid, whatever its ME networks are.
    """

    networks: list[MENetworkSpec] = Field(default_factory=list)
    power_external: bool = False

    @model_validator(mode="after")
    def _check(self) -> MEConfig:
        _check_networks(self.networks)
        return self

    def network(self, network_id: str) -> MENetworkSpec:
        """The network named ``network_id``; raises :class:`KeyError` for one this does not hold."""
        for net in self.networks:
            if net.id == network_id:
                return net
        raise KeyError(network_id)

    def colour(self, network_id: str) -> AEColor:
        """The cable colour of ``network_id``: Fluix when attached, else its own, else the first
        colour in AE2's order no subnet asked for, given out in network order."""
        spec = self.network(network_id)
        if spec.mode is MEMode.ATTACHED:
            return AEColor.FLUIX
        if spec.colour is not None:
            return spec.colour
        taken = {n.colour for n in self.networks if n.colour is not None}
        free = iter(c for c in AEColor if c is not AEColor.FLUIX and c not in taken)
        unset = [n.id for n in self.networks if n.mode is MEMode.SUBNET and n.colour is None]
        return dict(zip(unset, free, strict=False))[network_id]


class NetKind(str, Enum):
    """Where a net sits in a line: fed from its edge, delivering to it, or between two machines."""

    BOUNDARY_INPUT = "boundary_input"
    BOUNDARY_OUTPUT = "boundary_output"
    INTERNAL = "internal"


class NetEnd(FrozenModel):
    """One machine end of a listed net: a port on a machine, as a user picking ME needs to see it.

    ``suggested`` names the ME device that end would get on a default attached network
    (``dataset.me.me_devices_for``), or why none keeps up.
    """

    machine_id: str = Field(min_length=1)
    port_id: str = Field(min_length=1)
    machine_type: str = Field(min_length=1)
    multiblock: bool
    rate: float | None = Field(default=None, ge=0.0)
    suggested: str


class NetEntry(FrozenModel):
    """One item or fluid net a user may move to ME.

    ``producers`` and ``consumers`` are its machine ends; a boundary Super Chest or Super Tank is
    not an end, since riding ME replaces it (``kind`` says the net had one). ``rate`` is what the
    net carries per tick (items/t, mB/t).
    """

    id: str = Field(min_length=1)
    kind: NetKind
    commodity: Commodity
    resource: str = Field(min_length=1)
    resource_name: str | None = None
    rate: float = Field(ge=0.0)
    producers: tuple[NetEnd, ...] = ()
    consumers: tuple[NetEnd, ...] = ()

    @field_validator("commodity")
    @classmethod
    def _check_commodity(cls, value: Commodity) -> Commodity:
        if value is Commodity.POWER:
            raise ValueError("a power net never rides ME; only item and fluid nets are listed")
        return value


class NetList(StrictModel):
    """What ``gtnh-solve --list-nets`` prints: every net of a plan a user may move to ME.

    ``plan_digest`` is the SHA-256 of the plan as parsed, and ``dataset_version`` the identity of
    the physical dataset it was adapted against (``None`` without one): the net ids depend on both,
    so an :class:`MEPlan` must carry the same pair. ``line_tier`` is the highest voltage tier any of
    the plan's machines runs at, which decides what a tier-aware choice allows.
    """

    version: int = NETLIST_VERSION
    plan_digest: str = Field(min_length=1)
    dataset_version: str | None = None
    solver_version: str = Field(min_length=1)
    line_tier: str = Field(min_length=1)
    nets: list[NetEntry] = Field(default_factory=list)

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: int) -> int:
        return check_contract_version(value, NETLIST_VERSION, "NetList")


class MEPlan(StrictModel):
    """What ``gtnh-solve --me-plan`` reads: the ME networks, and which listed net rides which.

    ``nets`` maps a :class:`NetList` net id to one of ``networks``' ids; a net it does not name is
    piped. ``plan_digest`` and ``dataset_version`` are copied from the :class:`NetList` the choice
    was made against, and must still match the plan and dataset being solved.
    """

    version: int = ME_PLAN_VERSION
    plan_digest: str = Field(min_length=1)
    dataset_version: str | None = None
    networks: list[MENetworkSpec] = Field(default_factory=list)
    nets: dict[str, str] = Field(default_factory=dict)

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: int) -> int:
        return check_contract_version(value, ME_PLAN_VERSION, "MEPlan")

    @model_validator(mode="after")
    def _check(self) -> MEPlan:
        _check_networks(self.networks)
        known = {n.id for n in self.networks}
        unknown = sorted({network for network in self.nets.values() if network not in known})
        if unknown:
            raise ValueError(f"ME plan assigns nets to unknown ME network(s) {unknown}")
        return self


# --- what a problem asks the ME side to build (InputIR v9, #333) ----------------------------------


class MERole(str, Enum):
    """What an ME infrastructure machine is: a block the line needs for its network, serving no port.

    - ``attach``: the dense cable on the region's edge the player's main network enters through.
      Its front faces outside the build, like a power source's feed face, and the network's
      channels arrive through it (at most a dense cable's 32, and at most the network's budget);
    - ``link``: a cable bus on the region's edge carrying one storage bus on its front, facing out
      at the ME Interface the player places on their main network there; a ``link`` subnet's way
      to the player's storage. A cell outside the region touches one cell inside it at most, so
      one link carries one storage bus: a subnet moving items and fluids has two links, an item
      storage bus facing an ME Interface and a fluid one facing an ME Dual Interface, and costs the
      main network a channel for each;
    - ``controller``: an ME Controller block, which gives a subnet its channels;
    - ``acceptor``: an Energy Acceptor on the line's EU supply (#336).
    """

    ATTACH = "attach"
    LINK = "link"
    CONTROLLER = "controller"
    ACCEPTOR = "acceptor"


class MEDeviceSpec(FrozenModel):
    """The device an :class:`MEEndpoint` is built as: its kind, a GT ME hatch's mID, its cards, and
    ``config``, the resources it is set to (an export or import bus's filter, a storage bus's
    partition). Chosen by ``dataset.me.me_devices_for``."""

    kind: MEDeviceKind
    gt_mid: int | None = None
    cards: MECards = Field(default_factory=MECards)
    config: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> MEDeviceSpec:
        if (self.gt_mid is not None) is not self.kind.value.startswith("gt_"):
            raise ValueError("a GT ME hatch names its mID, and nothing else does")
        return self


class MEEndpoint(FrozenModel):
    """One ME device a machine needs: the ME counterpart of a hatch or a pipe's terminal.

    It serves ``ports``, ports of its machine whose nets ride ``network``: usually one, but a
    single block's Dual Interface takes its item and its fluid output together. An infrastructure
    machine's endpoint serves none: a link's storage bus (``MERole``). A port too fast for
    one device has two endpoints, each carrying ``share`` of the port's rate, the way a heavy
    machine's draw is split across energy hatches. ``hatch_kind`` is set on a multiblock: the kind
    of hatch slot the connection takes, the GT ME hatch itself or the normal hatch its AE2 part
    stands in front of.
    """

    id: str = Field(min_length=1)
    network: str = Field(min_length=1)
    ports: tuple[str, ...] = ()
    device: MEDeviceSpec
    hatch_kind: str | None = None
    share: float = Field(default=1.0, gt=0.0, le=1.0)

    @field_validator("ports")
    @classmethod
    def _check_ports(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("an ME endpoint's ports must not repeat")
        return value


# --- what a layout builds (LayoutResult v5, #333) ---------------------------------------------------


class MECableCell(FrozenModel):
    """One AE2 cable block of an ME network: where it is and what it is built from.

    ``me_channels`` is the solver's own count of the channel devices it routed through this cable,
    which the previewer lights; the validator re-derives it from the blocks themselves and never
    trusts it. An ``attach`` or ``link`` machine's cell is listed here too: those blocks are cables.
    """

    cell: CellCoord
    kind: MECableKind
    me_channels: int = Field(default=0, ge=0)


class MEPlacedDevice(FrozenModel):
    """One ME device a layout builds: an AE2 part on a cable, or one of GT's ME hatches.

    ``machine_id`` is the machine it serves, and ``endpoint_id`` the :class:`MEEndpoint` of that
    machine it builds (a ``link``'s storage bus included). For a **part**, ``cell`` is the cable
    block it sits on and ``side`` the side of that block
    it sits on, which faces the block it works on. For a **GT ME hatch**, ``cell`` is the casing
    cell the hatch takes and ``side`` the way its front faces, where AE reaches it.
    """

    machine_id: str = Field(min_length=1)
    endpoint_id: str = Field(min_length=1)
    kind: MEDeviceKind
    cell: CellCoord
    side: Facing
    gt_mid: int | None = None
    cards: MECards = Field(default_factory=MECards)
    config: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> MEPlacedDevice:
        if (self.gt_mid is not None) is not self.kind.value.startswith("gt_"):
            raise ValueError("a GT ME hatch names its mID, and nothing else does")
        return self


class MENetworkLayout(StrictModel):
    """One ME network as a layout builds it: its cable blocks and its devices.

    ``id`` names one of the problem's ``InputIR.me`` networks, and ``colour`` the colour its cables
    are built in. Its controller, acceptor, attach stub and link are machines, placed like any
    other (``Machine.me_role``); the stub and the link are cable blocks too, so their cells are
    in ``cables``. Which blocks actually join which network in game is not this record's to say: AE
    connects whatever compatible blocks touch, so the validator rebuilds that from the blocks.
    """

    id: str = Field(min_length=1)
    colour: AEColor
    cables: list[MECableCell] = Field(default_factory=list)
    devices: list[MEPlacedDevice] = Field(default_factory=list)

    def cells(self) -> set[tuple[int, int, int]]:
        """Every block this network's cables take: what any check of the ground a block stands on
        counts, beside each route's ``Route.cells``."""
        return {c.cell.as_tuple() for c in self.cables}

    @model_validator(mode="after")
    def _check(self) -> MENetworkLayout:
        cells = [c.cell for c in self.cables]
        if len(cells) != len(set(cells)):
            raise ValueError(f"ME network {self.id!r} lists a cable cell twice")
        return self


# --- what a layout reports about each network (LayoutResult v5, additive, #336) ---------------------


class MEFlowMetrics(FrozenModel):
    """One resource an ME network's storage must supply, or takes in: its ids (a merged run's
    several), what kind it is, and the summed rate of the nets that move it (items/t, mB/t)."""

    resources: tuple[str, ...] = Field(min_length=1)
    commodity: Commodity
    rate: float = Field(ge=0.0)

    @field_validator("commodity")
    @classmethod
    def _check_commodity(cls, value: Commodity) -> Commodity:
        if value is Commodity.POWER:
            raise ValueError("ME stores items and fluids, never power")
        return value


class MENetworkMetrics(StrictModel):
    """What one ME network of a layout asks of the player, for a reader of the layout alone.

    ``devices`` are its channel devices, each spending one channel; ``channel_budget`` what they
    may spend: an attached network's free channels on the main network, an ad-hoc subnet's 8, and
    ``None`` for a subnet with a controller, whose cables each carry 32 from it. ``main_channels``
    are the main network's channels it spends (an attached network one a device, a link subnet one
    a link). ``ae_per_tick`` is what it draws, computed from the cable the layout lays (spike 6),
    and ``eu_per_tick`` the same in EU: what an Energy Acceptor takes for it, or what the main
    network (attached) or a quartz fiber (an external subnet) must carry. ``flush_ae`` is the most
    one GT ME output bus or hatch of it spends in a single flush, which its energy store must hold
    (0 with none). ``supplies`` must be in its storage for the line to run, and ``absorbs`` lands
    there.
    """

    id: str = Field(min_length=1)
    mode: MEMode
    colour: AEColor
    power: MEPower
    devices: int = Field(ge=0)
    channel_budget: int | None = Field(default=None, ge=0)
    main_channels: int = Field(ge=0)
    ae_per_tick: float = Field(ge=0.0)
    eu_per_tick: float = Field(ge=0.0)
    flush_ae: float = Field(default=0.0, ge=0.0)
    supplies: list[MEFlowMetrics] = Field(default_factory=list)
    absorbs: list[MEFlowMetrics] = Field(default_factory=list)
