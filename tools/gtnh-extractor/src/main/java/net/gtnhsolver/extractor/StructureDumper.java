package net.gtnhsolver.extractor;

import java.io.File;
import java.lang.reflect.Field;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import net.minecraft.block.Block;
import net.minecraft.init.Blocks;
import net.minecraft.item.ItemStack;
import net.minecraft.tileentity.TileEntity;
import net.minecraft.world.World;
import net.minecraftforge.common.util.ForgeDirection;

import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import com.gtnewhorizon.structurelib.StructureLib;
import com.gtnewhorizon.structurelib.StructureLibAPI;
import com.gtnewhorizon.structurelib.alignment.IAlignment;
import com.gtnewhorizon.structurelib.alignment.constructable.ChannelDataAccessor;
import com.gtnewhorizon.structurelib.alignment.constructable.IConstructable;
import com.gtnewhorizon.structurelib.alignment.enumerable.ExtendedFacing;

import gregtech.api.GregTechAPI;
import gregtech.api.interfaces.IHeatingCoil;
import gregtech.api.interfaces.metatileentity.IMetaTileEntity;
import gregtech.api.metatileentity.BaseMetaTileEntity;
import gregtech.common.misc.GTStructureChannels;

/**
 * The core dump loop: iterate every registered GregTech meta tile entity, build the ones that can
 * construct themselves into a scratch region of a real server world, read back the placed blocks
 * and hint dots, and hand the raw facts to {@link JsonWriter}. No game logic beyond coordinate
 * collection lives here - footprint math, faces, and tier semantics are the Python adapter's job
 * (design principle 3 of {@code docs/dataset-extraction/plan.md}).
 *
 * <p>
 * Because we execute the very {@code construct(...)} the in-game hologram projector runs, the
 * output matches in-game behaviour by construction. Per controller and per trigger-stack size:
 *
 * <pre>
 *   place controller at a fixed origin, ExtendedFacing NORTH
 *        |
 *        v
 *   HINT pass  : construct(trigger, hintsOnly=true)  with a {@link RecordingProxy} swapped into
 *        |       StructureLib.proxy and world.isRemote flipped true (the hint walk is client-only)
 *        v       -> capture the hinted cells + the hint-block dots (legal hatch / DOF slots)
 *   wipe + re-place controller   (clears any terrain/residue so the scan sees only our build)
 *        |
 *        v
 *   BLOCK pass : construct(trigger, hintsOnly=false) -> real casing shell into the world
 *        |       (hatch slots fall back to their chained casing; gt_no_hatch keeps real hatches out)
 *        v
 *   scan the affected cube -> {d:[dx,dy,dz], block, meta} relative to the controller, plus bbox
 *        |
 *        v
 *   sweep trigger stack 1..N, stop when the occupied cell set stops changing -> one Variant per
 *   shape (identity-only tier swaps collapse; genuine size variants stay distinct)
 *        |
 *        v
 *   CHANNEL PROBE (lane 3): recover the tiers the shape-signature collapse discarded.
 * </pre>
 *
 * <p>
 * <b>Why one sweep already covers shape-changing channels.</b> StructureLib reads a channel with
 * {@code ChannelDataAccessor.getChannelData(trigger, channel)}, which falls back to the trigger's
 * <em>stack size</em> when the channel is unset. So the stack sweep above already varies every
 * channel at once: a channel that changes the <em>shape</em> (a distillation tower's {@code height},
 * a structure's {@code length}) yields distinct occupied-cell sets and is recorded as separate
 * variants; a channel that only swaps a tiered <em>block</em> (coil, glass, pipe casing) keeps the
 * same shape and collapses into one variant, throwing its tier information away.
 *
 * <p>
 * The channel probe recovers exactly that discarded tier information. Holding the stack size at 1
 * (every other channel at its default) it sets one channel at a time to values {@code 1..N} and
 * diffs the built blocks against the default build:
 *
 * <pre>
 *   for each GT channel (skip gt_no_hatch, which is always applied):
 *     set channel = 2..N, rebuild, compare occupied cells + block identity to the default build
 *       occupied cells changed  -> shape-changing channel: already a size variant above, skip here
 *       only block identity moved -> identity-only channel: record {channel_value, block, meta} for
 *                                    the default tier and every distinct tier -> substitutions[chan]
 *       nothing changed          -> controller does not use this channel, skip
 * </pre>
 *
 * The default-placed block is always included (it is the value-1 entry), which is what lets the
 * Python adapter match the tiered blocks in the primary variant.
 *
 * <p>
 * <b>Heating coils are a special case.</b> Only some coil multiblocks bind their coil element to the
 * {@code coil} channel (mega furnaces, via {@code HEATING_COIL.use(...)}); the classic ones (Electric
 * Blast Furnace, Multi Smelter, ...) place a bare {@code ofCoil} element whose tier is read straight
 * from the trigger's <em>stack size</em>, so an explicit {@code coil} channel does nothing. The coil
 * table is therefore built by a separate stack-size sweep that identifies coil blocks by the GT
 * {@code IHeatingCoil} interface the block itself implements (a fact the block declares, not a
 * hard-coded name). This one sweep covers both kinds, since a channel-bound coil falls back to the
 * stack size when its channel is unset.
 *
 * <p>
 * Every controller is wrapped so an exception, a non-terminating/explosive sweep, or an
 * over-cap channel/substitution space becomes a {@code _meta.json} failure instead of aborting the
 * run.
 */
final class StructureDumper {

    private static final Logger LOG = LogManager.getLogger(DumperMod.MODID);

    /** StructureLib channel that keeps auto-placed hatches out, leaving the casing shell + hints. */
    private static final String NO_HATCH_CHANNEL = "gt_no_hatch";

    /** The GT heating-coil channel name; coils are swept by stack size, so it is skipped in the loop. */
    private static final String COIL_CHANNEL = GTStructureChannels.HEATING_COIL.get();

    // Fixed scratch origin: high in the spawn chunks, well above terrain, so the region is empty
    // air we can build into and wipe freely. Offsets in the JSON are world deltas from here.
    private static final int OX = 8;
    private static final int OY = 210;
    private static final int OZ = 8;

    // Hard caps (plan risk 9.2): bound the trigger-stack sweep and the per-controller variant count
    // so a dynamic/explosive structure lands on the failure list rather than running away.
    //
    // MAX_VARIANTS was 6, which rejected the whole controller for 16 of 191 machines (GitHub #98).
    // That was the wrong instrument: 14 of those 16 are legitimately parametric families (a base
    // plus a repeated slice, driven by a StructureLib STRUCTURE_HEIGHT / STRUCTURE_LENGTH channel
    // that falls back to the trigger stack size), so a low variant count discarded real machines
    // rather than catching runaway ones. It was also redundant - the sweep cannot produce more than
    // MAX_STACK_SWEEP forms, and per-variant blowup is already bounded by MAX_CELLS + MAX_SCAN_DIM,
    // which cap the thing that actually costs (cells scanned), not the count of shapes. Pinning it
    // to MAX_STACK_SWEEP keeps the constant as documentation of that ceiling while making it
    // non-binding; the sweep's own stabilisation break is what ends a well-behaved family.
    private static final int MAX_STACK_SWEEP = 16;
    private static final int MAX_VARIANTS = MAX_STACK_SWEEP;
    private static final int MAX_CELLS = 20000;
    private static final int MAX_SCAN_DIM = 80;
    private static final int DEFAULT_SCAN_RADIUS = 12;

    // Lane 3 caps: bound the per-channel value sweep (14 coil tiers + margin) and the total number of
    // substitution entries a controller may emit, so a controller with a pathological channel space
    // lands on the failure list rather than emitting a runaway table.
    private static final int MAX_CHANNEL_VALUE = 16;
    private static final int MAX_SUBSTITUTION_ENTRIES = 128;

    private final World world;
    private final ErrorCollector errors = new ErrorCollector();
    private final Block hintBlock = StructureLibAPI.getBlockHint();

    // Set -PdebugMeta=<id> to dump what the hint pass captured for one controller (diagnostics only).
    private final int debugMeta = parseIntProp("gtnhextractor.debugMeta", -1);

    private Field proxyField;
    private Field isRemoteField;

    StructureDumper(World world) {
        this.world = world;
    }

    ErrorCollector errors() {
        return errors;
    }

    /** A single hint particle captured during the hint pass. {@code block} may be {@code null}. */
    private static final class Particle {

        final int x;
        final int y;
        final int z;
        final Block block;
        final int meta;

        Particle(int x, int y, int z, Block block, int meta) {
            this.x = x;
            this.y = y;
            this.z = z;
            this.block = block;
            this.meta = meta;
        }
    }

    /** Signals a controller could not be dumped; carries the one-line reason for the failure list. */
    private static final class DumpException extends Exception {

        DumpException(String message) {
            super(message);
        }
    }

    /**
     * Dump every constructable controller into {@code datasetOut/multiblocks/}. Returns the number
     * of controllers successfully written; failures are available from {@link #errors()}.
     */
    int run(File datasetOut, String packVersion, java.util.Map<String, String> modVersions, String extractorSha)
        throws java.io.IOException {
        File multiblocksDir = new File(datasetOut, "multiblocks");
        multiblocksDir.mkdirs();

        preloadRegion();

        JsonWriter writer = new JsonWriter();
        IMetaTileEntity[] metaTileEntities = GregTechAPI.METATILEENTITIES;
        int written = 0;
        int considered = 0;

        for (int id = 0; id < metaTileEntities.length; id++) {
            IMetaTileEntity imte = metaTileEntities[id];
            if (imte == null || !(imte instanceof IConstructable)) {
                continue;
            }
            considered++;
            String registryName = "gregtech:meta." + id;
            try {
                DumpModel.MultiblockDoc doc = dumpController(imte, id);
                registryName = doc.controller.registryName + "#" + doc.controller.meta;
                writer.writeDoc(multiblocksDir, doc);
                written++;
                DumpModel.Variant primary = doc.variants.get(doc.variants.size() - 1);
                LOG.debug(
                    "gtnh-extractor: dumped {} '{}' variants={} blocks={} hints={} bbox={}",
                    registryName,
                    doc.controller.displayName,
                    doc.variants.size(),
                    primary.blocks.size(),
                    primary.hints.size(),
                    java.util.Arrays.toString(primary.bbox));
            } catch (DumpException e) {
                errors.record(registryName, e.getMessage());
            } catch (Throwable t) {
                errors.record(registryName, t);
            } finally {
                safeWipe(defaultCube());
            }
        }

        String generatedAt = java.time.Instant.now()
            .toString();
        writer
            .writeMeta(multiblocksDir, packVersion, modVersions, generatedAt, extractorSha, written, errors.failures());

        LOG.info(
            "gtnh-extractor: {} constructable controllers considered, {} dumped, {} failed.",
            considered,
            written,
            errors.count());
        return written;
    }

    /**
     * Force every chunk in the scratch working area into the loaded map up front, so nothing during
     * the dump triggers on-demand chunk generation. A {@code getBlock} that loads a fresh chunk
     * mid-dump runs terrain decoration, and a shared biome decorator re-entered that way throws
     * {@code "Already decorating!!"} - which, once a controller's many probe builds provoke it, would
     * corrupt later controllers too. Loading every working chunk here (each lands in the loaded map
     * even if its own one-time decoration throws, and the scratch region sits at Y=210 above any
     * decoration) keeps the whole dump reading only already-resident chunks.
     */
    private void preloadRegion() {
        int radius = 6; // chunks around the origin: covers the widest structure plus scan/wipe margins
        int ocx = OX >> 4;
        int ocz = OZ >> 4;
        int loaded = 0;
        for (int cx = ocx - radius; cx <= ocx + radius; cx++) {
            for (int cz = ocz - radius; cz <= ocz + radius; cz++) {
                try {
                    world.getBlock((cx << 4) + 8, OY, (cz << 4) + 8);
                    loaded++;
                } catch (Throwable t) {
                    // A cascade during this one-time preload is harmless: the chunk still lands in the
                    // loaded map, so no dump-time getBlock has to regenerate it.
                }
            }
        }
        LOG.info("gtnh-extractor: preloaded {} scratch chunks (radius {} around the origin).", loaded, radius);
    }

    /** Dump one controller: sweep the trigger stack and collect one variant per distinct form. */
    private DumpModel.MultiblockDoc dumpController(IMetaTileEntity imte, int id) throws DumpException {
        ItemStack form = imte.getStackForm(1);
        if (form == null || form.getItem() == null) {
            throw new DumpException("controller has no item form");
        }
        Block machineBlock = Block.getBlockFromItem(form.getItem());
        if (machineBlock == null || machineBlock == Blocks.air) {
            throw new DumpException("controller has no block form");
        }
        Object nameObj = Block.blockRegistry.getNameForObject(machineBlock);
        String registryName = nameObj != null ? nameObj.toString() : "unknown";
        String displayName = displayName(imte);
        String facingConvention = "controller front = NORTH (-Z), ExtendedFacing "
            + ExtendedFacing.of(ForgeDirection.NORTH)
            + "; offsets d = [dx,dy,dz] world-space deltas from the controller block";

        DumpModel.Controller controller = new DumpModel.Controller(
            registryName,
            id,
            displayName,
            imte.getClass()
                .getName(),
            facingConvention);
        DumpModel.MultiblockDoc doc = new DumpModel.MultiblockDoc(controller);

        LinkedHashMap<String, DumpModel.Variant> distinct = new LinkedHashMap<>();
        String previousSignature = null;
        // Whether the sweep ENDED ON ITS OWN (the shape stopped changing, or the controller stopped
        // building) rather than simply running out of stack sizes. A family whose range outruns
        // MAX_STACK_SWEEP - the Lapotronic Supercapacitor spans heights 4..50, the Power Station
        // Control Node 4..18 - leaves this false, and what we dumped is a PREFIX of the real family.
        boolean stabilised = false;
        for (int n = 1; n <= MAX_STACK_SWEEP; n++) {
            DumpModel.Variant variant = buildVariant(imte, id, machineBlock, registryName, n);
            if (variant.blocks.size() < 2) {
                if (n == 1) {
                    throw new DumpException("empty scan (no structure built in the void world)");
                }
                stabilised = true;
                break; // stopped producing a structure at this stack size
            }
            String signature = signature(variant);
            if (signature.equals(previousSignature)) {
                stabilised = true;
                break; // occupied cell set stopped changing -> the sweep has stabilised
            }
            if (!distinct.containsKey(signature)) {
                distinct.put(signature, variant);
                if (distinct.size() > MAX_VARIANTS) {
                    throw new DumpException("variant sweep exceeded the cap of " + MAX_VARIANTS + " forms");
                }
            }
            previousSignature = signature;
        }
        if (distinct.isEmpty()) {
            throw new DumpException("empty scan (no structure built in the void world)");
        }
        if (!stabilised) {
            // Record rather than throw: a prefix of a parametric family still renders and still
            // reserves a legal footprint, which beats dropping the controller entirely. But a
            // consumer picking "the largest form" would otherwise silently believe it had found the
            // maximum, so the truncation has to be visible in the doc.
            doc.failures.add(
                "variant family truncated: the shape was still changing at the trigger-stack ceiling of "
                    + MAX_STACK_SWEEP + ", so forms beyond " + distinct.size() + " were not swept");
        }
        doc.variants.addAll(distinct.values());
        // The first variant is the smallest stack size (1), i.e. the structure at its default tiers;
        // probe channels against it to recover the identity-substitution tables the sweep collapsed.
        probeChannels(
            imte,
            id,
            distinct.values()
                .iterator()
                .next(),
            doc);
        return doc;
    }

    /**
     * Build one variant for trigger-stack size {@code n}: hint pass, block pass, scan. The scratch
     * region is wiped before the block pass (clearing any terrain) and again on the way out.
     */
    private DumpModel.Variant buildVariant(IMetaTileEntity imte, int id, Block machineBlock, String registryName, int n)
        throws DumpException {
        // Hint pass runs with a plain trigger so the hologram shows its hatch dots; the block pass
        // runs with gt_no_hatch so no real hatch tile entity is auto-placed (leaving the casing shell).
        ItemStack hintTrigger = imte.getStackForm(n);
        if (hintTrigger == null) {
            hintTrigger = imte.getStackForm(1);
        }
        ItemStack blockTrigger = hintTrigger.copy();
        ChannelDataAccessor.setChannelData(blockTrigger, NO_HATCH_CHANNEL, 1);

        int[] cube = defaultCube();
        try {
            IConstructable controller = placeController(imte, id);

            List<Particle> particles = hintPass(controller, hintTrigger);
            cube = scanCube(particles);
            if (id == debugMeta) {
                logHintDiagnostics(id, n, particles);
            }

            safeWipe(cube);
            controller = placeController(imte, id);
            controller.construct(blockTrigger, false);

            DumpModel.Variant variant = new DumpModel.Variant(n);
            variant.channels.put(NO_HATCH_CHANNEL, 1);
            scanBlocks(cube, machineBlock, id, variant);
            if (variant.blocks.size() < 2) {
                fallbackBlocksFromHints(particles, registryName, variant);
            }
            collectHints(particles, variant);
            computeBbox(variant);
            return variant;
        } finally {
            safeWipe(cube);
        }
    }

    /** One scanned cell's identity, keyed by position in a block map. Equality is (block, meta). */
    private static final class Cell {

        final String block;
        final int meta;

        Cell(String block, int meta) {
            this.block = block;
            this.meta = meta;
        }

        @Override
        public boolean equals(Object o) {
            if (this == o) {
                return true;
            }
            if (!(o instanceof Cell)) {
                return false;
            }
            Cell other = (Cell) o;
            return meta == other.meta && block.equals(other.block);
        }

        @Override
        public int hashCode() {
            return 31 * block.hashCode() + meta;
        }
    }

    /**
     * Probe every GT channel against the default build and fill {@code doc.substitutions} with the
     * identity-only channels (coil/glass/pipe-casing tiers). Shape-changing channels are left to the
     * trigger-stack sweep (an unset channel reads the stack size, so the sweep already varies them);
     * channels the controller does not use produce nothing. Best-effort: a channel that throws mid
     * sweep just stops, and only an over-cap substitution space fails the whole controller.
     */
    private void probeChannels(IMetaTileEntity imte, int id, DumpModel.Variant base, DumpModel.MultiblockDoc doc)
        throws DumpException {
        ItemStack baseTrigger = imte.getStackForm(1);
        if (baseTrigger == null) {
            return; // no item form to carry channel data; the base variant is already recorded
        }
        ChannelDataAccessor.setChannelData(baseTrigger, NO_HATCH_CHANNEL, 1);
        int[] cube = probeCube(base);
        Map<Long, Cell> baseline = buildBlockMap(imte, id, baseTrigger, cube);
        if (baseline == null || baseline.size() < 2) {
            return; // nothing solid to compare tiers against (e.g. a hint-derived shell)
        }
        Set<Long> baseCells = baseline.keySet();

        int totalEntries = 0;
        // Coils first, by their own stack-size sweep (they are not driven by the coil channel on the
        // classic furnaces). The channel loop then skips the coil channel so it is recorded once.
        List<DumpModel.Substitution> coils = probeCoils(imte, id, baseline, cube);
        if (!coils.isEmpty()) {
            doc.substitutions.put(COIL_CHANNEL, coils);
            totalEntries += coils.size();
        }
        for (GTStructureChannels channel : GTStructureChannels.values()) {
            String name = channel.get();
            if (name == null || name.equals(NO_HATCH_CHANNEL) || name.equals(COIL_CHANNEL)) {
                continue; // gt_no_hatch is always applied; coils are handled by the stack-size sweep
            }
            List<DumpModel.Substitution> entries = probeChannel(imte, id, name, baseTrigger, baseline, baseCells, cube);
            if (entries.isEmpty()) {
                continue;
            }
            doc.substitutions.put(name, entries);
            totalEntries += entries.size();
            if (totalEntries > MAX_SUBSTITUTION_ENTRIES) {
                throw new DumpException(
                    "substitution table exceeded the cap of " + MAX_SUBSTITUTION_ENTRIES + " entries");
            }
        }
        if (id == debugMeta && !doc.substitutions.isEmpty()) {
            LOG.info("gtnh-extractor DEBUG meta {}: substitutions {}", id, doc.substitutions.keySet());
        }
    }

    /**
     * Enumerate a controller's heating-coil tiers into a {@code coil} substitution list. Runs only if
     * the default build already placed a coil (a block implementing {@link IHeatingCoil}); then it
     * sweeps the trigger stack size, which is what the coil element reads for its tier, and records
     * each distinct coil block. Covers both the classic furnaces (bare {@code ofCoil}, stack-size
     * driven) and the channel-bound mega furnaces (which fall back to the stack size when the coil
     * channel is unset), so it is the single source of the coil table for both.
     */
    private List<DumpModel.Substitution> probeCoils(IMetaTileEntity imte, int id, Map<Long, Cell> baseline,
        int[] cube) {
        boolean baseHasCoil = false;
        for (Cell c : baseline.values()) {
            if (isCoilBlock(c.block)) {
                baseHasCoil = true;
                break;
            }
        }
        if (!baseHasCoil) {
            return new ArrayList<>(); // no coil in this structure; nothing to enumerate
        }
        List<DumpModel.Substitution> entries = new ArrayList<>();
        Set<String> seen = new LinkedHashSet<>();
        Set<Cell> previousCoils = null;
        for (int n = 1; n <= MAX_CHANNEL_VALUE; n++) {
            ItemStack trigger = imte.getStackForm(n);
            if (trigger == null) {
                break;
            }
            ChannelDataAccessor.setChannelData(trigger, NO_HATCH_CHANNEL, 1);
            Map<Long, Cell> map = buildBlockMap(imte, id, trigger, cube);
            if (map == null) {
                break;
            }
            Set<Cell> coils = new LinkedHashSet<>();
            for (Cell c : map.values()) {
                if (isCoilBlock(c.block)) {
                    coils.add(c);
                }
            }
            if (coils.isEmpty()) {
                break;
            }
            if (coils.equals(previousCoils)) {
                break; // the tier clamped at its maximum; the sweep has stabilised
            }
            for (Cell c : coils) {
                if (seen.add(n + " " + c.block + " " + c.meta)) {
                    entries.add(new DumpModel.Substitution(n, c.block, c.meta));
                }
            }
            previousCoils = coils;
        }
        return entries;
    }

    /** Whether a registry name resolves to a block that declares itself a GT heating coil. */
    private boolean isCoilBlock(String registryName) {
        try {
            return Block.getBlockFromName(registryName) instanceof IHeatingCoil;
        } catch (Throwable t) {
            return false;
        }
    }

    /**
     * Sweep one channel's values at stack size 1, holding every other channel at its default, and
     * return the identity-substitution entries for it. Returns an empty list when the channel is
     * unused (no build differs from the default) or shape-changing (the occupied cells move, which
     * the trigger-stack sweep already captured as a size variant).
     */
    private List<DumpModel.Substitution> probeChannel(IMetaTileEntity imte, int id, String name, ItemStack baseTrigger,
        Map<Long, Cell> baseline, Set<Long> baseCells, int[] cube) {
        // builds: channel value -> block map. Seed the default build as value 1, because an unset
        // channel reads the stack size (1 here), so the default tier is exactly value 1.
        LinkedHashMap<Integer, Map<Long, Cell>> builds = new LinkedHashMap<>();
        builds.put(1, baseline);
        Map<Long, Cell> previous = baseline;
        for (int value = 2; value <= MAX_CHANNEL_VALUE; value++) {
            ItemStack trigger = baseTrigger.copy();
            ChannelDataAccessor.setChannelData(trigger, name, value);
            Map<Long, Cell> map = buildBlockMap(imte, id, trigger, cube);
            if (map == null || !map.keySet()
                .equals(baseCells)) {
                // The build failed, or the occupied cells moved: not identity-only. A genuine shape
                // change is a size variant the stack sweep already enumerated, so stop probing here.
                break;
            }
            if (map.equals(previous)) {
                break; // the tier clamped past its maximum; the sweep has stabilised
            }
            builds.put(value, map);
            previous = map;
        }
        // Controlled cells: those whose block identity moves as the channel changes (vs the default).
        Set<Long> controlled = new LinkedHashSet<>();
        for (Map.Entry<Integer, Map<Long, Cell>> build : builds.entrySet()) {
            if (build.getKey() == 1) {
                continue; // value 1 is the default itself; nothing differs from it yet
            }
            for (Long cell : baseCells) {
                if (!baseline.get(cell)
                    .equals(
                        build.getValue()
                            .get(cell))) {
                    controlled.add(cell);
                }
            }
        }
        if (controlled.isEmpty()) {
            return new ArrayList<>(); // channel unused by this controller (or a single default tier)
        }
        // One entry per (value, distinct block+meta) at the controlled cells, the default included.
        List<DumpModel.Substitution> entries = new ArrayList<>();
        Set<String> seen = new LinkedHashSet<>();
        for (Map.Entry<Integer, Map<Long, Cell>> build : builds.entrySet()) {
            int value = build.getKey();
            Set<Cell> tierBlocks = new LinkedHashSet<>();
            for (Long cell : controlled) {
                Cell c = build.getValue()
                    .get(cell);
                if (c != null) {
                    tierBlocks.add(c);
                }
            }
            for (Cell c : tierBlocks) {
                if (seen.add(value + " " + c.block + " " + c.meta)) {
                    entries.add(new DumpModel.Substitution(value, c.block, c.meta));
                }
            }
        }
        return entries;
    }

    /**
     * A block pass with the given trigger, scanned into a position -> {@link Cell} map over
     * {@code cube}. Used only for channel probing (no hint pass): the wipe/place/construct/scan is
     * self-contained and always cleans up the scratch region. Returns {@code null} if the build
     * threw, so a channel value that a controller rejects just ends that channel's sweep.
     */
    private Map<Long, Cell> buildBlockMap(IMetaTileEntity imte, int id, ItemStack trigger, int[] cube) {
        try {
            IConstructable controller = placeController(imte, id);
            controller.construct(trigger, false);
            Map<Long, Cell> map = new HashMap<>();
            for (int x = cube[0]; x <= cube[3]; x++) {
                for (int y = cube[1]; y <= cube[4]; y++) {
                    for (int z = cube[2]; z <= cube[5]; z++) {
                        Block block = world.getBlock(x, y, z);
                        if (block == null || block == Blocks.air || block == hintBlock) {
                            continue;
                        }
                        Object name = Block.blockRegistry.getNameForObject(block);
                        if (name == null) {
                            continue;
                        }
                        map.put(packKey(x, y, z), new Cell(name.toString(), world.getBlockMetadata(x, y, z)));
                    }
                }
            }
            return map;
        } catch (Throwable t) {
            return null;
        } finally {
            // Wipe the scan region unioned with the default cube. Bounded to what phase 1 already
            // touched (never a fixed margin beyond it): reaching into an ungenerated chunk here forces
            // mid-dump terrain decoration, which throws "Already decorating!!" and corrupts the run.
            safeWipe(unionCube(cube, defaultCube()));
        }
    }

    /**
     * The world cube to scan while probing: the default variant's block span expanded by one, capped
     * to {@link #MAX_SCAN_DIM} and the world height. Tight enough that identity swaps are cheap to
     * compare, yet a shape-changing channel still shows a moved (or added) cell so it can be told
     * apart from an identity-only one. Not clamped to the default cube, so large tiered structures
     * whose coils sit outside a 12-block radius still have every tier captured.
     */
    private int[] probeCube(DumpModel.Variant base) {
        int minDx = Integer.MAX_VALUE;
        int minDy = Integer.MAX_VALUE;
        int minDz = Integer.MAX_VALUE;
        int maxDx = Integer.MIN_VALUE;
        int maxDy = Integer.MIN_VALUE;
        int maxDz = Integer.MIN_VALUE;
        for (DumpModel.PlacedBlock b : base.blocks) {
            minDx = Math.min(minDx, b.dx);
            maxDx = Math.max(maxDx, b.dx);
            minDy = Math.min(minDy, b.dy);
            maxDy = Math.max(maxDy, b.dy);
            minDz = Math.min(minDz, b.dz);
            maxDz = Math.max(maxDz, b.dz);
        }
        int minX = OX + minDx - 1;
        int minY = Math.max(OY + minDy - 1, 0);
        int minZ = OZ + minDz - 1;
        int maxX = Math.min(OX + maxDx + 1, minX + MAX_SCAN_DIM - 1);
        int maxY = Math.min(Math.min(OY + maxDy + 1, minY + MAX_SCAN_DIM - 1), 255);
        int maxZ = Math.min(OZ + maxDz + 1, minZ + MAX_SCAN_DIM - 1);
        return new int[] { minX, minY, minZ, maxX, maxY, maxZ };
    }

    /** The smallest world cube covering both {@code a} and {@code b}. */
    private int[] unionCube(int[] a, int[] b) {
        return new int[] { Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.min(a[2], b[2]), Math.max(a[3], b[3]),
            Math.max(a[4], b[4]), Math.max(a[5], b[5]) };
    }

    /** Place the controller at the origin with a known facing and return it as an IConstructable. */
    private IConstructable placeController(IMetaTileEntity imte, int id) throws DumpException {
        world.setBlock(OX, OY, OZ, blockOf(imte), 0, 3);
        TileEntity te = world.getTileEntity(OX, OY, OZ);
        if (!(te instanceof BaseMetaTileEntity)) {
            throw new DumpException("no BaseMetaTileEntity at origin after setBlock");
        }
        BaseMetaTileEntity base = (BaseMetaTileEntity) te;
        base.setMetaTileID((short) id);
        IMetaTileEntity controller = imte.newMetaEntity(base);
        if (controller == null) {
            throw new DumpException("newMetaEntity returned null");
        }
        base.setMetaTileEntity(controller);
        controller.setBaseMetaTileEntity(base);
        applyFacing(controller, base);
        if (!(controller instanceof IConstructable)) {
            throw new DumpException("controller instance is not IConstructable");
        }
        return (IConstructable) controller;
    }

    private Block blockOf(IMetaTileEntity imte) throws DumpException {
        ItemStack form = imte.getStackForm(1);
        Block block = form != null && form.getItem() != null ? Block.getBlockFromItem(form.getItem()) : null;
        if (block == null || block == Blocks.air) {
            throw new DumpException("controller has no block form");
        }
        return block;
    }

    /** Point the controller front at NORTH so the offset frame is deterministic and documented. */
    private void applyFacing(IMetaTileEntity controller, BaseMetaTileEntity base) {
        base.setFrontFacing(ForgeDirection.NORTH);
        ExtendedFacing facing = ExtendedFacing.of(ForgeDirection.NORTH);
        Field field = findField(controller.getClass(), "mExtendedFacing");
        if (field != null) {
            try {
                field.setAccessible(true);
                field.set(controller, facing);
                return;
            } catch (Throwable ignored) {
                // fall through to the interface setter
            }
        }
        if (controller instanceof IAlignment) {
            try {
                ((IAlignment) controller).setExtendedFacing(facing);
            } catch (Throwable ignored) {
                // leave the constructor default; construct will use whatever facing is present
            }
        }
    }

    /**
     * Run the hints-only construct with a {@link RecordingProxy} temporarily installed as
     * StructureLib's proxy, capturing every hinted cell. The swap is a reflective one-field touch of
     * an internal StructureLib static; a version bump that moves it fails here loudly and locally.
     */
    private List<Particle> hintPass(IConstructable controller, ItemStack trigger) throws DumpException {
        List<Particle> particles = new ArrayList<>();
        RecordingProxy recorder = new RecordingProxy((w, x, y, z, block, meta) -> {
            if (particles.size() < MAX_CELLS) {
                particles.add(new Particle(x, y, z, block, meta));
            }
        });
        Field field = proxyField();
        Object original;
        try {
            original = field.get(null);
        } catch (IllegalAccessException e) {
            throw new DumpException("cannot read StructureLib.proxy: " + e.getMessage());
        }
        // StructureLib's hint walk is client-only: iterate() opens with
        // `if (!world.isRemote && hintsOnly) return false;`. On a dedicated server isRemote is false,
        // so construct(_, true) would no-op. Flip isRemote to true just for this synchronous, non-
        // ticking hint pass (nothing else touches the world meanwhile), then restore it.
        boolean flipped = setRemote(true);
        try {
            field.set(null, recorder);
            try {
                controller.construct(trigger, true);
            } catch (Throwable t) {
                // Hints are best-effort: pretending to be a client (isRemote=true) makes some
                // elements' spawnHint reach client-only icon rendering (NoSuchMethodError on the
                // server). Keep whatever dots were captured before the throw; the authoritative
                // geometry still comes from the block pass, so the controller is not lost.
                LOG.debug("gtnh-extractor: hint pass aborted early ({} dots so far)", particles.size());
            }
        } catch (IllegalAccessException e) {
            throw new DumpException("cannot swap StructureLib.proxy: " + e.getMessage());
        } finally {
            try {
                field.set(null, original);
            } catch (IllegalAccessException ignored) {
                // best effort restore; a failure here would surface on the next hint pass
            }
            if (flipped) {
                setRemote(false);
            }
        }
        if (particles.size() >= MAX_CELLS) {
            throw new DumpException("structure too large (" + MAX_CELLS + "+ hinted cells)");
        }
        return particles;
    }

    private Field proxyField() throws DumpException {
        if (proxyField == null) {
            try {
                proxyField = StructureLib.class.getDeclaredField("proxy");
                proxyField.setAccessible(true);
            } catch (NoSuchFieldException e) {
                throw new DumpException("StructureLib.proxy field is gone: " + e.getMessage());
            }
        }
        return proxyField;
    }

    /**
     * Toggle the world's {@code isRemote} client/server flag (a non-static final read fresh on every
     * hint call, so a reflective write takes effect immediately). Returns whether the value actually
     * changed, so the caller only restores what it altered. A failure leaves hints empty, not fatal.
     */
    private boolean setRemote(boolean value) {
        try {
            if (isRemoteField == null) {
                isRemoteField = World.class.getDeclaredField("isRemote");
                isRemoteField.setAccessible(true);
            }
            if (isRemoteField.getBoolean(world) == value) {
                return false;
            }
            isRemoteField.setBoolean(world, value);
            return true;
        } catch (ReflectiveOperationException e) {
            LOG.warn("gtnh-extractor: cannot toggle world.isRemote; hints will be empty", e);
            return false;
        }
    }

    /**
     * The world-coordinate cube to scan: the hinted cells expanded by one, but never smaller than
     * the default radius around the origin. The floor matters when the hint pass aborts early (a
     * client-only icon error) and captured few or no cells - the block pass still built the whole
     * structure, so the scan must cover it regardless of how far the hologram got.
     */
    private int[] scanCube(List<Particle> particles) {
        int minX = OX - DEFAULT_SCAN_RADIUS;
        int minY = OY - DEFAULT_SCAN_RADIUS;
        int minZ = OZ - DEFAULT_SCAN_RADIUS;
        int maxX = OX + DEFAULT_SCAN_RADIUS;
        int maxY = OY + DEFAULT_SCAN_RADIUS;
        int maxZ = OZ + DEFAULT_SCAN_RADIUS;
        for (Particle p : particles) {
            minX = Math.min(minX, p.x - 1);
            minY = Math.min(minY, p.y - 1);
            minZ = Math.min(minZ, p.z - 1);
            maxX = Math.max(maxX, p.x + 1);
            maxY = Math.max(maxY, p.y + 1);
            maxZ = Math.max(maxZ, p.z + 1);
        }
        maxX = Math.min(maxX, minX + MAX_SCAN_DIM - 1);
        maxY = Math.min(maxY, minY + MAX_SCAN_DIM - 1);
        maxZ = Math.min(maxZ, minZ + MAX_SCAN_DIM - 1);
        minY = Math.max(minY, 0);
        maxY = Math.min(maxY, 255);
        return new int[] { minX, minY, minZ, maxX, maxY, maxZ };
    }

    private int[] defaultCube() {
        return new int[] { OX - DEFAULT_SCAN_RADIUS, OY - DEFAULT_SCAN_RADIUS, OZ - DEFAULT_SCAN_RADIUS,
            OX + DEFAULT_SCAN_RADIUS, OY + DEFAULT_SCAN_RADIUS, OZ + DEFAULT_SCAN_RADIUS };
    }

    /** Read every non-air block in the cube into the variant, controller-relative; force the origin. */
    private void scanBlocks(int[] cube, Block machineBlock, int machineMeta, DumpModel.Variant variant) {
        for (int x = cube[0]; x <= cube[3]; x++) {
            for (int y = cube[1]; y <= cube[4]; y++) {
                for (int z = cube[2]; z <= cube[5]; z++) {
                    Block block = world.getBlock(x, y, z);
                    if (block == null || block == Blocks.air || block == hintBlock) {
                        continue;
                    }
                    if (x == OX && y == OY && z == OZ) {
                        continue; // origin is forced to the controller below
                    }
                    Object name = Block.blockRegistry.getNameForObject(block);
                    if (name == null) {
                        continue;
                    }
                    variant.blocks.add(
                        new DumpModel.PlacedBlock(
                            x - OX,
                            y - OY,
                            z - OZ,
                            name.toString(),
                            world.getBlockMetadata(x, y, z)));
                }
            }
        }
        Object machineName = Block.blockRegistry.getNameForObject(machineBlock);
        if (machineName != null) {
            variant.blocks.add(new DumpModel.PlacedBlock(0, 0, 0, machineName.toString(), machineMeta));
        }
    }

    /**
     * When the real block pass built nothing (a controller that needs world context a void world
     * lacks), recover the shell from the hologram the hint pass captured: every hinted cell whose
     * block is a real block (not the hint block) is a solid cell the projector would show.
     */
    private void fallbackBlocksFromHints(List<Particle> particles, String registryName, DumpModel.Variant variant) {
        variant.blocks.clear();
        Set<Long> seen = new LinkedHashSet<>();
        for (Particle p : particles) {
            if (p.block == null || p.block == hintBlock || p.block == Blocks.air) {
                continue;
            }
            long key = packKey(p.x, p.y, p.z);
            if (!seen.add(key)) {
                continue;
            }
            Object name = Block.blockRegistry.getNameForObject(p.block);
            if (name == null) {
                continue;
            }
            variant.blocks.add(new DumpModel.PlacedBlock(p.x - OX, p.y - OY, p.z - OZ, name.toString(), p.meta));
        }
        if (!variant.blocks.isEmpty()) {
            LOG.debug(
                "gtnh-extractor: {} recovered {} blocks from the hint pass (void-world build)",
                registryName,
                variant.blocks.size());
        }
    }

    /** Collect the hint-block dots (legal hatch / DOF slots) as controller-relative positions. */
    private void collectHints(List<Particle> particles, DumpModel.Variant variant) {
        Set<Long> seen = new LinkedHashSet<>();
        for (Particle p : particles) {
            if (p.block != hintBlock) {
                continue;
            }
            if (!seen.add(packKey(p.x, p.y, p.z))) {
                continue;
            }
            variant.hints.add(new DumpModel.HintDot(p.x - OX, p.y - OY, p.z - OZ, p.meta));
        }
    }

    /** Derive the bounding box from the block span so it agrees with the adapter's cross-check. */
    private void computeBbox(DumpModel.Variant variant) {
        if (variant.blocks.isEmpty()) {
            return;
        }
        int minX = Integer.MAX_VALUE;
        int minY = Integer.MAX_VALUE;
        int minZ = Integer.MAX_VALUE;
        int maxX = Integer.MIN_VALUE;
        int maxY = Integer.MIN_VALUE;
        int maxZ = Integer.MIN_VALUE;
        for (DumpModel.PlacedBlock b : variant.blocks) {
            minX = Math.min(minX, b.dx);
            minY = Math.min(minY, b.dy);
            minZ = Math.min(minZ, b.dz);
            maxX = Math.max(maxX, b.dx);
            maxY = Math.max(maxY, b.dy);
            maxZ = Math.max(maxZ, b.dz);
        }
        variant.bbox = new int[] { maxX - minX + 1, maxY - minY + 1, maxZ - minZ + 1 };
    }

    /** Set every non-air block in the cube back to air, clearing the controller and its structure. */
    private void safeWipe(int[] cube) {
        try {
            for (int x = cube[0]; x <= cube[3]; x++) {
                for (int y = Math.max(cube[1], 0); y <= Math.min(cube[4], 255); y++) {
                    for (int z = cube[2]; z <= cube[5]; z++) {
                        if (world.getBlock(x, y, z) != Blocks.air) {
                            world.setBlock(x, y, z, Blocks.air, 0, 2);
                        }
                    }
                }
            }
        } catch (Throwable t) {
            LOG.warn("gtnh-extractor: wipe failed around the scratch origin", t);
        }
    }

    /**
     * A variant's <em>shape</em> signature: the set of occupied cells only, deliberately ignoring
     * block identity and meta. The trigger stack selects tier variants (coil/glass/pipe casings)
     * that keep the same shape but swap the block; those are identity-only and belong in the lane 3
     * substitution table, not as separate variants, so they collapse here. A genuine size variant
     * (a distillation-tower layer, a taller structure) changes the occupied cells and stays distinct.
     */
    private String signature(DumpModel.Variant variant) {
        List<String> rows = new ArrayList<>(variant.blocks.size());
        for (DumpModel.PlacedBlock b : variant.blocks) {
            rows.add(b.dx + "," + b.dy + "," + b.dz);
        }
        rows.sort(null);
        return String.join(";", rows);
    }

    /** Log what the hint pass captured for one controller: total cells, dots, and block-name tallies. */
    private void logHintDiagnostics(int id, int n, List<Particle> particles) {
        int dots = 0;
        java.util.Map<String, Integer> byBlock = new java.util.TreeMap<>();
        for (Particle p : particles) {
            if (p.block == hintBlock) {
                dots++;
            }
            String key = p.block == null ? "<icon-only>"
                : String.valueOf(Block.blockRegistry.getNameForObject(p.block));
            byBlock.merge(key, 1, Integer::sum);
        }
        LOG.info(
            "gtnh-extractor DEBUG meta {} n={}: {} particles, {} hint-dots, blocks={}",
            id,
            n,
            particles.size(),
            dots,
            byBlock);
    }

    private static int parseIntProp(String key, int fallback) {
        try {
            String value = System.getProperty(key);
            return value != null ? Integer.parseInt(value.trim()) : fallback;
        } catch (NumberFormatException e) {
            return fallback;
        }
    }

    private static long packKey(int x, int y, int z) {
        return ((long) (x & 0x1FFFFF) << 42) | ((long) (y & 0x1FFFFF) << 21) | (z & 0x1FFFFF);
    }

    private static String displayName(IMetaTileEntity imte) {
        String name = null;
        try {
            name = imte.getLocalName();
        } catch (Throwable ignored) {
            // fall back to the internal name below
        }
        if (name == null || name.trim()
            .isEmpty()) {
            try {
                name = imte.getMetaName();
            } catch (Throwable ignored) {
                name = null;
            }
        }
        if (name == null || name.trim()
            .isEmpty()) {
            name = imte.getClass()
                .getSimpleName();
        }
        return name;
    }

    private static Field findField(Class<?> type, String name) {
        for (Class<?> c = type; c != null && c != Object.class; c = c.getSuperclass()) {
            try {
                return c.getDeclaredField(name);
            } catch (NoSuchFieldException ignored) {
                // keep walking up the hierarchy
            }
        }
        return null;
    }
}
