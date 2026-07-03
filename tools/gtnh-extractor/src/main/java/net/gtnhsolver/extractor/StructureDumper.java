package net.gtnhsolver.extractor;

import java.io.File;
import java.lang.reflect.Field;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
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
import gregtech.api.interfaces.metatileentity.IMetaTileEntity;
import gregtech.api.metatileentity.BaseMetaTileEntity;

/**
 * The core dump loop: iterate every registered GregTech meta tile entity, build the ones that can
 * construct themselves into a scratch region of a real server world, read back the placed blocks
 * and hint dots, and hand the raw facts to {@link JsonWriter}. No game logic beyond coordinate
 * collection lives here - footprint math, faces, and tier semantics are the Python adapter's job
 * (design principle 3 of {@code DATASET_EXTRACTION_PLAN.md}).
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
 * </pre>
 *
 * Every controller is wrapped so an exception, a non-terminating/explosive sweep, or an empty scan
 * becomes a {@code _meta.json} failure instead of aborting the run.
 */
final class StructureDumper {

    private static final Logger LOG = LogManager.getLogger(DumperMod.MODID);

    /** StructureLib channel that keeps auto-placed hatches out, leaving the casing shell + hints. */
    private static final String NO_HATCH_CHANNEL = "gt_no_hatch";

    // Fixed scratch origin: high in the spawn chunks, well above terrain, so the region is empty
    // air we can build into and wipe freely. Offsets in the JSON are world deltas from here.
    private static final int OX = 8;
    private static final int OY = 210;
    private static final int OZ = 8;

    // Hard caps (plan risk 9.2): bound the trigger-stack sweep and the per-controller variant count
    // so a dynamic/explosive structure lands on the failure list rather than running away.
    private static final int MAX_STACK_SWEEP = 16;
    private static final int MAX_VARIANTS = 6;
    private static final int MAX_CELLS = 20000;
    private static final int MAX_SCAN_DIM = 80;
    private static final int DEFAULT_SCAN_RADIUS = 12;

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
        for (int n = 1; n <= MAX_STACK_SWEEP; n++) {
            DumpModel.Variant variant = buildVariant(imte, id, machineBlock, registryName, n);
            if (variant.blocks.size() < 2) {
                if (n == 1) {
                    throw new DumpException("empty scan (no structure built in the void world)");
                }
                break; // stopped producing a structure at this stack size
            }
            String signature = signature(variant);
            if (signature.equals(previousSignature)) {
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
        doc.variants.addAll(distinct.values());
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
