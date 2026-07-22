package net.gtnhsolver.extractor;

import java.io.File;
import java.io.IOException;
import java.io.Writer;
import java.lang.invoke.MethodHandle;
import java.lang.invoke.MethodHandles;
import java.lang.invoke.MethodType;
import java.lang.reflect.Field;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.TreeSet;

import net.minecraft.block.Block;
import net.minecraft.init.Blocks;
import net.minecraft.item.Item;
import net.minecraft.item.ItemStack;
import net.minecraft.tileentity.TileEntity;
import net.minecraft.util.IIcon;
import net.minecraft.world.World;
import net.minecraftforge.common.util.ForgeDirection;

import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonPrimitive;

import cpw.mods.fml.common.registry.FMLControlledNamespacedRegistry;
import cpw.mods.fml.common.registry.GameData;
import gregtech.api.GregTechAPI;
import gregtech.api.enums.Textures;
import gregtech.api.interfaces.IIconContainer;
import gregtech.api.interfaces.ITexture;
import gregtech.api.interfaces.metatileentity.IMetaTileEntity;
import gregtech.api.interfaces.tileentity.IGregTechTileEntity;
import gregtech.api.metatileentity.BaseMetaTileEntity;
import gregtech.api.metatileentity.implementations.MTEBasicMachine;

/**
 * The texture pass, v2 (lane 6 v2, issue #79). Emits the <b>layered</b> texture manifest (plan
 * section 5.4): for every block a preview draws, the ordered bottom-to-top {@code ITexture} layer
 * stack per side and per active state, each layer resolved to an iconset name + RGBA tint + glow
 * flag. This supersedes v1's flat single-icon Option A, which could only name casing shells and
 * gapped every single-block machine and controller hull.
 *
 * <p>
 * <b>Two mechanisms, composed</b> (both proven by the lane S spike, #78):
 * <ul>
 * <li><b>MTE reflection</b> for machines, hatches, buses, and multiblock controller hulls: their
 * {@code ITexture[]} is obtained server-side - via the {@code getXxxFacing(byte)} accessors for
 * basic single-block machines (no tile entity), and via
 * {@code getTexture(base, side, facing, colour, active, redstone)} for the rest (placed like the
 * StructureDumper does). Each layer is a {@code GTRenderedTexture} ({@code mIconContainer} +
 * {@code getRGBA()} + {@code glow}), a sided/multi wrapper (recursed via {@code mTextures}), or a
 * {@code GTCopiedBlockTextureRender} whose copied casing icon is resolved via the block-icon path.
 * A basic machine's accessors return the casing layer ONLY (its {@code mTextures} stack, overlays
 * included, is built {@code @SideOnly(CLIENT)} and is null on the dedicated server), so its
 * per-machine front glyph is reconstructed from the deterministic {@code basicmachines/<folder>/}
 * asset path (see the "Basic-machine per-face overlays" section).
 * <li><b>Block-icon reflection</b> (v1's mechanism, kept) for the plain structure blocks a
 * multiblock places - casings, coils, tiered glass: a single un-tinted iconset layer per meta.
 * </ul>
 *
 * <p>
 * The {@code getTextureFile()} accessor is {@code @SideOnly(CLIENT)} and throws on the server, so
 * icon names come from the {@code Textures.BlockIcons} enum {@code name()} (or a custom container's
 * {@code mIconName} field), which map 1:1 to the PNGs under {@code assets/<modid>/textures/blocks/}.
 * No PNG is read or written here; the previewer fetches them from the Nexus jar at preview time.
 */
final class TextureDumper {

    private static final Logger LOG = LogManager.getLogger(DumperMod.MODID);

    /** Layered-manifest schema version. Bump when the on-disk shape changes. */
    static final int SCHEMA_VERSION = 2;

    private static final String[] GET_ICON_NAMES = { "getIcon", "func_149691_a" };
    private static final String ICON_DOMAIN = "gregtech";

    /** ForgeDirection names in ordinal order (0 DOWN .. 5 EAST); the manifest keys sides by these. */
    private static final String[] SIDE_NAMES = { "DOWN", "UP", "NORTH", "SOUTH", "WEST", "EAST" };

    // Scratch origin for placing hull/hatch MTEs to read their getTexture (mirrors StructureDumper).
    private static final int OX = 8;
    private static final int OY = 210;
    private static final int OZ = 8;

    private final Gson gson = new GsonBuilder().setPrettyPrinting()
        .disableHtmlEscaping()
        .create();

    private final World world;

    /** icon name -> jar asset path, accumulated across every resolved layer. */
    private final Map<String, String> icons = new TreeMap<>();
    /** unresolved (block, meta, side, reason) units, surfaced in the manifest diff. */
    private final List<Gap> gaps = new ArrayList<>();

    private String lastIconError;

    TextureDumper(World world) {
        this.world = world;
    }

    /** One resolved texture layer: iconset name, RGBA multiply (r,g,b,a 0-255), and the glow flag. */
    private static final class Layer {

        final String icon;
        final int[] rgba;
        final boolean glow;

        Layer(String icon, int[] rgba, boolean glow) {
            this.icon = icon;
            this.rgba = rgba;
            this.glow = glow;
        }
    }

    /** One (block, meta, side) the pass could not resolve to a layer. */
    private static final class Gap {

        final String block;
        final int meta;
        final String side;
        final String reason;

        Gap(String block, int meta, String side, String reason) {
            this.block = block;
            this.meta = meta;
            this.side = side;
            this.reason = reason;
        }
    }

    /** A manifest block entry: its kind, display name (MTEs), source class, and per-side/state layers. */
    private static final class Entry {

        final String kind;
        final String displayName;
        final String sourceClass;
        // side name -> state ("inactive"/"active") -> ordered layer list
        final Map<String, Map<String, List<Layer>>> sides = new TreeMap<>();

        Entry(String kind, String displayName, String sourceClass) {
            this.kind = kind;
            this.displayName = displayName;
            this.sourceClass = sourceClass;
        }
    }

    /**
     * Build the layered manifest and write {@code <textureOut>/manifest.json}. Returns the number of
     * resolved (block, side, state) layer stacks, so a run that resolves nothing fails CI loudly.
     */
    int run(File textureOut, String packVersion, Map<String, String> modVersions, String extractorSha)
        throws IOException {
        textureOut.mkdirs();
        populateIconNames();
        verifyCasingTable();
        enumerateBasicMachineOverlays();

        Map<String, Entry> blocks = new TreeMap<>();
        int mteStacks = dumpMetaTileEntities(blocks);
        int blockStacks = dumpPlainBlocks(blocks);

        writeManifest(new File(textureOut, "manifest.json"), packVersion, modVersions, extractorSha, blocks);
        int total = mteStacks + blockStacks;
        LOG.info(
            "gtnh-extractor: texture manifest v{} wrote {} block entries ({} MTE + plain stacks), "
                + "{} icons, {} gaps.",
            SCHEMA_VERSION,
            blocks.size(),
            total,
            icons.size(),
            gaps.size());
        return total;
    }

    // ------------------------------------------------------------------------------------------
    // Basic-machine per-face overlays (issue #3)
    // ------------------------------------------------------------------------------------------
    //
    // A basic single-block machine's per-machine glyph (the hammer, the macerator blades) is NOT
    // reachable server-side: the whole textured stack (mTextures) is built @SideOnly(CLIENT) and is
    // null on the dedicated server, and the getXxxFacing accessors return the casing layer only. But
    // the overlay lives at a deterministic asset path,
    //   assets/gregtech/textures/blocks/basicmachines/<folder>/OVERLAY_<FACE>[_ACTIVE][_GLOW].png
    // and the <folder> is derivable from the machine's server-side mName (basicmachine.<token>.tier.NN)
    // by matching <token> to the real folder set (folder key = folder with non-alphanumerics stripped;
    // the 53 GT5U folders are collision-free under that key). We enumerate that folder set (and every
    // OVERLAY_* file in it) once from the GT5U jar on the classpath, then reconstruct the overlay layer
    // for the faces that actually have a PNG - never inventing one.

    /** normalised machine token -> real overlay folder name (e.g. "alloysmelter" -> "alloy_smelter"). */
    private final Map<String, String> overlayFolders = new LinkedHashMap<>();
    /** every existing "<folder>/OVERLAY_..." asset (without the .png), for O(1) presence checks. */
    private final TreeSet<String> overlayAssets = new TreeSet<>();

    /** OVERLAY_&lt;FACE&gt; token per render side (0 DOWN .. 5 EAST); the front is NORTH. */
    private static final String[] OVERLAY_FACE = { "BOTTOM", "TOP", "FRONT", "SIDE", "SIDE", "SIDE" };

    private static final String BASICMACHINES_PREFIX = "assets/" + ICON_DOMAIN
        + "/textures/blocks/basicmachines/";

    /**
     * Enumerate every {@code basicmachines/<folder>/OVERLAY_*.png} on the classpath into
     * {@link #overlayFolders} + {@link #overlayAssets}. The assets can live in a different classpath
     * entry than the deobfuscated GT classes, so the containing jar/dir is found by resolving a known
     * overlay resource URL rather than a GT class's CodeSource. Best-effort: if nothing resolves,
     * basic machines simply keep their casing-only faces (no overlay is invented).
     */
    private void enumerateBasicMachineOverlays() {
        try {
            ClassLoader cl = MTEBasicMachine.class.getClassLoader();
            java.net.URL boot = null;
            for (String probe : new String[] { "hammer", "macerator", "compressor", "extractor" }) {
                boot = cl.getResource(BASICMACHINES_PREFIX + probe + "/OVERLAY_FRONT.png");
                if (boot != null) {
                    break;
                }
            }
            if (boot == null) {
                LOG.warn("gtnh-extractor: no basicmachines overlay assets on the classpath; skipping enumeration");
                return;
            }
            LOG.info("gtnh-extractor: basicmachines overlays resolved from {}", boot);
            if ("jar".equals(boot.getProtocol())) {
                enumerateOverlaysFromJar(boot);
            } else if ("file".equals(boot.getProtocol())) {
                enumerateOverlaysFromDir(boot);
            } else {
                LOG.warn("gtnh-extractor: unsupported overlay asset URL protocol {}", boot.getProtocol());
            }
        } catch (Throwable t) {
            LOG.warn("gtnh-extractor: could not enumerate basicmachines overlays: {}", t.toString());
        }
        LOG.info("gtnh-extractor: enumerated {} basicmachines overlay folders ({} files)", overlayFolders.size(),
            overlayAssets.size());
    }

    /** Enumerate the jar behind a {@code jar:file:...!/...} overlay resource URL. */
    private void enumerateOverlaysFromJar(java.net.URL bootResource) throws Exception {
        java.net.JarURLConnection conn = (java.net.JarURLConnection) bootResource.openConnection();
        File jar = new File(conn.getJarFileURL().toURI());
        try (java.util.zip.ZipFile zf = new java.util.zip.ZipFile(jar)) {
            java.util.Enumeration<? extends java.util.zip.ZipEntry> e = zf.entries();
            while (e.hasMoreElements()) {
                recordOverlayEntry(e.nextElement().getName());
            }
        }
    }

    /** Enumerate the exploded {@code .../basicmachines/} directory a {@code file:} resource URL sits in. */
    private void enumerateOverlaysFromDir(java.net.URL bootResource) throws Exception {
        // .../basicmachines/<folder>/OVERLAY_FRONT.png -> walk up to the shared basicmachines dir.
        File basicmachines = new File(bootResource.toURI()).getParentFile().getParentFile();
        File[] folders = basicmachines.listFiles(File::isDirectory);
        if (folders == null) {
            return;
        }
        for (File folder : folders) {
            File[] pngs = folder.listFiles((d, n) -> n.endsWith(".png"));
            if (pngs == null) {
                continue;
            }
            for (File png : pngs) {
                recordOverlayEntry(BASICMACHINES_PREFIX + folder.getName() + "/" + png.getName());
            }
        }
    }

    /** Record one {@code assets/.../basicmachines/<folder>/OVERLAY_*.png} entry into the lookup maps. */
    private void recordOverlayEntry(String name) {
        if (!name.startsWith(BASICMACHINES_PREFIX) || !name.endsWith(".png")) {
            return;
        }
        String rel = name.substring(BASICMACHINES_PREFIX.length()); // "<folder>/OVERLAY_FRONT.png"
        int slash = rel.indexOf('/');
        if (slash <= 0) {
            return;
        }
        overlayFolders.putIfAbsent(normalizeToken(rel.substring(0, slash)), rel.substring(0, slash));
        overlayAssets.add(rel.substring(0, rel.length() - ".png".length())); // "<folder>/OVERLAY_FRONT"
    }

    /** Lower-case and strip non-alphanumerics, so {@code alloy_smelter} and {@code alloysmelter} unify. */
    private static String normalizeToken(String s) {
        return s.toLowerCase(java.util.Locale.ROOT).replaceAll("[^a-z0-9]", "");
    }

    /** The overlay folder for a basic machine, from its {@code mName} (basicmachine.&lt;token&gt;.tier.NN). */
    private String overlayFolderFor(MTEBasicMachine mte) {
        Object nameObj = readField(mte, "mName");
        if (!(nameObj instanceof String)) {
            return null;
        }
        String mName = (String) nameObj;
        String marker = ".tier.";
        int tierAt = mName.lastIndexOf(marker);
        if (!mName.startsWith("basicmachine.") || tierAt < 0) {
            return null;
        }
        String token = mName.substring("basicmachine.".length(), tierAt);
        return overlayFolders.get(normalizeToken(token));
    }

    /**
     * Append the reconstructed overlay layer(s) for one folder + face/state to {@code layers}: the
     * glyph itself, then - if a {@code _GLOW} sibling PNG exists - a separate emissive layer above it,
     * exactly as the enum-overlay path represents a glowing front (casing, overlay, {@code _GLOW}).
     * Nothing is appended if the machine has no overlay PNG for this face (never invented). Layers are
     * untinted and fully opaque (rgba {@code [255,255,255,0]}, the OVERLAY_SCHEST convention).
     */
    private void appendOverlayLayers(List<Layer> layers, String folder, int side, boolean active) {
        String base = "OVERLAY_" + OVERLAY_FACE[side] + (active ? "_ACTIVE" : "");
        String rel = folder + "/" + base;
        if (!overlayAssets.contains(rel)) {
            return;
        }
        layers.add(overlayLayer(rel, false));
        if (overlayAssets.contains(rel + "_GLOW")) {
            layers.add(overlayLayer(rel + "_GLOW", true));
        }
    }

    /** One overlay layer for a {@code <folder>/OVERLAY_...} asset, registering its icon -> jar path. */
    private Layer overlayLayer(String rel, boolean glow) {
        return new Layer(registerIcon(ICON_DOMAIN, "basicmachines/" + rel), new int[] { 255, 255, 255, 0 }, glow);
    }

    // ------------------------------------------------------------------------------------------
    // MTE layer reflection (machines, hatches, buses, controller hulls)
    // ------------------------------------------------------------------------------------------

    /** Reflect the layered textures of every registered MetaTileEntity into {@code blocks}. */
    private int dumpMetaTileEntities(Map<String, Entry> blocks) {
        IMetaTileEntity[] all = GregTechAPI.METATILEENTITIES;
        int stacks = 0;
        for (int id = 0; id < all.length; id++) {
            IMetaTileEntity imte = all[id];
            if (imte == null) {
                continue;
            }
            try {
                stacks += dumpOneMTE(imte, id, blocks);
            } catch (Throwable t) {
                gaps.add(new Gap("meta." + id, id, "all", "MTE dump threw " + t.getClass().getSimpleName()));
            } finally {
                wipe();
            }
        }
        return stacks;
    }

    /** Dump one MTE: resolve its block+meta key, display name, and per-side/state layer stacks. */
    private int dumpOneMTE(IMetaTileEntity imte, int id, Map<String, Entry> blocks) {
        ItemStack form = imte.getStackForm(1);
        Block block = form != null && form.getItem() != null ? Block.getBlockFromItem(form.getItem()) : null;
        if (block == null || block == Blocks.air) {
            return 0;
        }
        Object nameObj = Block.blockRegistry.getNameForObject(block);
        if (nameObj == null) {
            return 0;
        }
        String key = nameObj + "|" + id;
        Entry entry = new Entry("mte", safeName(imte), imte.getClass().getName());

        boolean basic = imte instanceof MTEBasicMachine;
        // Non-basic MTEs (hulls/hatches) read their layers off a live getTexture, so place ONCE and
        // reuse the base TE for all 12 side/state queries instead of re-placing per query.
        IMetaTileEntity placed = null;
        IGregTechTileEntity placedBase = null;
        if (!basic) {
            try {
                placed = place(imte, id);
                placedBase = (IGregTechTileEntity) placed.getBaseMetaTileEntity();
            } catch (Throwable t) {
                gaps.add(new Gap(nameObj.toString(), id, "all", "place threw " + t.getClass().getSimpleName()));
                return 0;
            }
        }

        int stacks = 0;
        for (int side = 0; side < 6; side++) {
            Map<String, List<Layer>> perState = new TreeMap<>();
            for (boolean active : new boolean[] { false, true }) {
                List<Layer> layers = basic
                    ? basicMachineLayers((MTEBasicMachine) imte, side, active)
                    : getTextureLayers(placed, placedBase, side, active, nameObj.toString(), id);
                if (layers != null && !layers.isEmpty()) {
                    perState.put(active ? "active" : "inactive", layers);
                }
            }
            if (!perState.isEmpty()) {
                entry.sides.put(SIDE_NAMES[side], perState);
                stacks += perState.size();
            }
        }
        if (!entry.sides.isEmpty()) {
            blocks.put(key, entry);
        } else {
            // getTexture answered, but nothing in it resolved to a layer - so this MTE is dropped.
            // Without this the drop was SILENT: describe() files its complaint under the offending
            // ITexture CLASS name, so nothing anywhere named the block, and 29 multiblock controller
            // hulls (Eye of Harmony, Forge of the Gods, the Space Modules) went missing from the
            // manifest with no recorded reason. A gap keyed by the block is what makes them findable.
            gaps.add(new Gap(nameObj.toString(), id, "all",
                "resolved no layer on any side (" + safeName(imte) + "; last: " + lastIconError + ")"));
        }
        return stacks;
    }

    /**
     * A basic single-block machine's layer stack for one side/state: the base casing (from the
     * {@code getXxxFacing} accessors, which return the casing layer only) with the per-machine
     * overlay glyph reconstructed on top from its deterministic asset path (see the
     * "Basic-machine per-face overlays" section). {@code mTextures} - the stack GT actually renders -
     * is null on the dedicated server, so the overlay cannot be read off it; it is rebuilt instead.
     * Machines with no overlay PNG for this face keep a casing-only stack, so nothing is invented.
     */
    private List<Layer> basicMachineLayers(MTEBasicMachine mte, int side, boolean active) {
        List<Layer> layers = basicMachineCasingLayers(mte, side, active);
        if (layers == null || layers.isEmpty()) {
            return layers; // casing did not resolve; an overlay without a casing under it makes no sense
        }
        String folder = overlayFolderFor(mte);
        if (folder != null) {
            appendOverlayLayers(layers, folder, side, active);
        }
        return layers;
    }

    /** The base casing layer(s) via the {@code getXxxFacing} accessors (colour -1 = unpainted steel). */
    private List<Layer> basicMachineCasingLayers(MTEBasicMachine mte, int side, boolean active) {
        String accessor;
        switch (side) {
            case 0:
                accessor = active ? "getBottomFacingActive" : "getBottomFacingInactive";
                break;
            case 1:
                accessor = active ? "getTopFacingActive" : "getTopFacingInactive";
                break;
            case 2:
                accessor = active ? "getFrontFacingActive" : "getFrontFacingInactive";
                break;
            default:
                accessor = active ? "getSideFacingActive" : "getSideFacingInactive";
        }
        try {
            Object result = MTEBasicMachine.class.getMethod(accessor, byte.class).invoke(mte, (byte) -1);
            if (result instanceof ITexture[]) {
                return describeAll((ITexture[]) result, side);
            }
        } catch (Throwable t) {
            lastIconError = accessor + " threw " + t.getClass().getSimpleName();
        }
        return null;
    }

    /** Read an already-placed hull/hatch MTE's {@code getTexture} layer stack for one side/state. */
    private List<Layer> getTextureLayers(IMetaTileEntity mte, IGregTechTileEntity base, int side, boolean active,
        String registryName, int id) {
        try {
            ITexture[] layers = mte.getTexture(base, ForgeDirection.getOrientation(side), ForgeDirection.NORTH, -1,
                active, false);
            return layers == null ? null : describeAll(layers, side);
        } catch (Throwable t) {
            if (side == 0 && active) {
                gaps.add(new Gap(registryName, id, "all", "getTexture threw " + t.getClass().getSimpleName()));
            }
            return null;
        }
    }

    /** Describe an {@code ITexture[]} into a flat ordered layer list for {@code renderSide}. */
    private List<Layer> describeAll(ITexture[] textures, int renderSide) {
        List<Layer> out = new ArrayList<>();
        for (ITexture t : textures) {
            describe(t, renderSide, out, 0);
        }
        return out;
    }

    /**
     * Recursively flatten one {@code ITexture} into {@code out}: unwrap multi/sided wrappers via
     * {@code mTextures}, resolve a rendered leaf to {icon, rgba, glow}, and a copied-block leaf via
     * the block-icon path. Unknown implementations are recorded once as a gap, never invented.
     */
    private void describe(ITexture t, int renderSide, List<Layer> out, int depth) {
        if (t == null || depth > 6) {
            return;
        }
        Object nested = readField(t, "mTextures");
        if (nested instanceof ITexture[]) {
            ITexture[] inner = (ITexture[]) nested;
            // A sided wrapper holds one sub-texture per side; a multi wrapper stacks layers. Tell them
            // apart by length: exactly 6 means sided (pick this side), else composite every layer.
            if (inner.length == 6) {
                describe(inner[renderSide], renderSide, out, depth + 1);
            } else {
                for (ITexture sub : inner) {
                    describe(sub, renderSide, out, depth + 1);
                }
            }
            return;
        }
        Object iconContainer = readField(t, "mIconContainer");
        if (iconContainer instanceof IIconContainer) {
            String name = iconName((IIconContainer) iconContainer);
            if (name != null) {
                out.add(new Layer(name, rgbaOf(t), boolField(t, "glow")));
            }
            return;
        }
        Object copiedBlock = readField(t, "mBlock");
        if (copiedBlock instanceof Block) {
            NamedIcon icon = copiedIcon((Block) copiedBlock, intField(t, "mMeta"), intField(t, "mSide"), renderSide);
            if (icon != null) {
                out.add(new Layer(icon.iconName, new int[] { 255, 255, 255, 255 }, false));
            }
            return;
        }
        // An ITexture shape this flattener does not know. Record it, never guess - but record it
        // with enough detail to act on. "unknown ITexture class" alone cost a source dive to
        // diagnose and still left GT's OWN GTRenderedTexture unexplained, so the gap now carries the
        // instance fields that were actually present: that names the field this code should be
        // reading, straight out of the run log, without another archaeology pass. Deduped by class,
        // since one unhandled shape otherwise files the same complaint hundreds of times.
        if (unknownTextures.add(t.getClass().getName())) {
            gaps.add(new Gap(t.getClass().getName(), -1, SIDE_NAMES[renderSide],
                "unknown ITexture class (instance fields: " + instanceFields(t) + ")"));
        }
    }

    /** Classes already reported as an unknown {@code ITexture} shape, so each is recorded once. */
    private final TreeSet<String> unknownTextures = new TreeSet<>();

    /**
     * Every instance field on {@code o} and its ancestors as {@code name=<runtime value class>}, for
     * diagnosing an unknown shape.
     *
     * <p>
     * Reports the VALUE's class, not the declared type: the field names alone said both unhandled
     * shapes carry a perfectly ordinary {@code mIconContainer:IIconContainer}, which tells you the
     * flattener looked in the right place and still came away with nothing. Whether that is a null,
     * or an object of some class that does not implement the interface we compile against, is the
     * whole question - and only the runtime value answers it.
     */
    private static String instanceFields(Object o) {
        TreeSet<String> out = new TreeSet<>();
        for (Class<?> c = o.getClass(); c != null && c != Object.class; c = c.getSuperclass()) {
            for (Field f : c.getDeclaredFields()) {
                if (java.lang.reflect.Modifier.isStatic(f.getModifiers())) {
                    continue;
                }
                String value;
                try {
                    f.setAccessible(true);
                    Object v = f.get(o);
                    value = v == null ? "null" : v.getClass().getName();
                } catch (Throwable t) {
                    value = "<unreadable: " + t.getClass().getSimpleName() + ">";
                }
                out.add(f.getName() + "=" + value);
            }
        }
        return String.join(", ", out);
    }

    /**
     * An {@code IIconContainer}'s domain-qualified iconset name: the {@code BlockIcons} enum
     * {@code name()} (always the {@code gregtech} domain), else a custom container's
     * {@code mIconName}.
     *
     * <p>
     * The two custom-container families answer {@code mIconName} differently, and assuming either
     * one is {@code gregtech}-relative is what put 46 unfetchable paths in the shipped manifest:
     * <ul>
     * <li>GT's own {@code Textures.BlockIcons.CustomIcon} stores an <b>already domain-qualified</b>
     * name ({@code "gregtech:icons/NeutronActivator_Off"}), because its constructor runs a bare name
     * through {@code GregTech.getResourcePath} first. Prefixing that again produced
     * {@code gregtech:gregtech:icons/...}, an asset path with a literal colon in it (29 icons).
     * <li>GT++'s {@code TexturesGtBlock.CustomIcon} stores a <b>bare</b> name next to a separate
     * {@code mModID} field ({@code "miscutils"} for its single-arg constructor). Assuming
     * {@code gregtech} pointed 17 icons at {@code assets/gregtech/textures/blocks/TileEntities/},
     * a directory GT5U does not have (17 icons).
     * </ul>
     * One jar still serves both: GT5-Unofficial is a monorepo and ships all 20 asset domains.
     */
    private String iconName(IIconContainer c) {
        String[] ref = iconRef(c);
        if (ref == null) {
            gaps.add(new Gap(c.getClass().getName(), -1, "all", "unresolvable IIconContainer"));
            return null;
        }
        return registerIcon(ref[0], ref[1]);
    }

    /**
     * The {@code {domain, relative-path}} an icon container names, or null if it names nothing.
     *
     * <p>
     * A {@code BlockIcons} enum constant is its own name under {@code gregtech:iconsets/}. Every
     * other container carries an {@code mIconName}, and the two families spell it differently - see
     * {@link #iconName} for what assuming either one cost the shipped manifest.
     */
    private static String[] iconRef(Object container) {
        if (container instanceof Enum) {
            return new String[] { ICON_DOMAIN, "iconsets/" + ((Enum<?>) container).name() };
        }
        Object mIconName = readField(container, "mIconName");
        if (!(mIconName instanceof String) || ((String) mIconName).isEmpty()) {
            return null;
        }
        Object modId = readField(container, "mModID");
        String fallback = modId instanceof String && !((String) modId).isEmpty() ? (String) modId : ICON_DOMAIN;
        return splitIconName((String) mIconName, fallback);
    }

    /**
     * Split a raw icon name into {@code {domain, relative-path}}, falling back to
     * {@code fallbackDomain} when the name carries no domain of its own.
     *
     * <p>
     * Mirrors GT's own {@code getResourceLocation}: a one-character prefix is a Windows drive
     * letter, not a mod domain, so it resolves to {@code minecraft} rather than to {@code "c"}.
     */
    private static String[] splitIconName(String raw, String fallbackDomain) {
        int colon = raw.indexOf(':');
        if (colon >= 0) {
            return new String[] { colon > 1 ? raw.substring(0, colon) : "minecraft", raw.substring(colon + 1) };
        }
        return new String[] { fallbackDomain, raw };
    }

    /** Register {@code <domain>:<rel>} against its jar asset path and return the icon name. */
    private String registerIcon(String domain, String rel) {
        String name = domain + ":" + rel;
        icons.putIfAbsent(name, assetPath(domain, rel));
        return name;
    }

    /** The path a {@code <domain>:<rel>} block icon occupies inside its mod jar. */
    private static String assetPath(String domain, String rel) {
        return "assets/" + domain + "/textures/blocks/" + rel + ".png";
    }

    /**
     * The icon a block NAMES in its own server-readable state, or null if it names none.
     *
     * <p>
     * The fourth route, and the cheapest. A great many blocks put the {@code @SideOnly} on the
     * <b>resolved</b> icon array while the strings that NAME those icons are un-annotated and
     * survive on a dedicated server untouched:
     *
     * <pre>
     * &#64;SideOnly(Side.CLIENT) protected IIcon[] texture;   // stripped
     * String[] textureNames;                             // survives, set from the constructor
     * </pre>
     *
     * That is bartworks' shape (its {@code textureNames} hold fully-qualified paths like
     * {@code "bartworks:BoronSilicateGlassBlock"}), and GoodGenerator uses the same field name.
     * Vanilla does the equivalent one level up: {@code Block.setBlockTextureName} and the
     * {@code textureName} field it writes are both un-annotated, which is how gtnhlanth's casings
     * name themselves.
     *
     * <p>
     * <b>Read the field, never the getter.</b> {@code Block.getTextureName()} <i>is</i>
     * {@code @SideOnly} and is deleted server-side, so going through it fails while the field behind
     * it sits there perfectly readable. Getting that backwards costs a whole dump run to notice.
     *
     * <p>
     * A name that carries no domain of its own is resolved against the block's own registry domain,
     * which is right by construction: a mod's block names a texture in its own assets.
     */
    private NamedIcon namedTextureIcon(Block block, String registryName, int meta, int side) {
        String raw = rawTextureName(block, meta, side);
        if (raw == null) {
            return null;
        }
        // The domain is lower-cased because a mod id need not be: GoodGenerator's is literally
        // "GoodGenerator", while its assets live under assets/goodgenerator/. GT resolves the same
        // way (Mods.resourceDomain lower-cases), so following it keeps every path fetchable.
        String[] ref = splitIconName(raw, registryDomain(registryName));
        String domain = ref[0].toLowerCase(java.util.Locale.ROOT);
        return new NamedIcon(domain + ":" + ref[1], assetPath(domain, ref[1]));
    }

    /**
     * The raw name a block gives its {@code (meta, side)} texture, or null if it names none.
     *
     * <p>
     * Three field shapes, all un-annotated and so all readable server-side. The per-side pair comes
     * first because a block that has it also inherits an unset {@code textureNames}, so checking the
     * single array first would read a null and give up on a block that does name itself.
     */
    private static String rawTextureName(Block block, int meta, int side) {
        Object topDown = readField(block, "textureTopAndDown");
        Object walls = readField(block, "textureSide");
        if (topDown instanceof String[] && walls instanceof String[]) {
            return clampedName((String[]) (side < 2 ? topDown : walls), meta);
        }
        Object perMeta = readField(block, "textureNames");
        if (perMeta instanceof String[]) {
            return clampedName((String[]) perMeta, meta);
        }
        Object single = readField(block, "textureName");
        return single instanceof String && !((String) single).isEmpty() ? (String) single : null;
    }

    /**
     * {@code names[meta]}, falling back to index 0 when the meta is past the end - which mirrors the
     * {@code meta < length ? meta : 0} clamp the stripped {@code getIcon} bodies themselves use, so a
     * family that names one texture for many metas resolves them all instead of only meta 0.
     */
    private static String clampedName(String[] names, int meta) {
        if (names.length == 0) {
            return null;
        }
        String name = names[meta >= 0 && meta < names.length ? meta : 0];
        return name == null || name.isEmpty() ? null : name;
    }

    /**
     * The per-meta RGB tint a block carries alongside its texture names, or null for untinted.
     *
     * <p>
     * Read off the {@code color} FIELD, never the {@code getColor(int)} accessor: that method is
     * un-annotated and looks callable, but its body dereferences the {@code @SideOnly IIcon[]} and so
     * dies with NoSuchFieldError on a server. The field holds the same values with none of the risk.
     * A null row is legitimate (bartworks' fake glasses pass one), and means untinted.
     */
    private static int[] namedTextureTint(Block block, int meta) {
        Object colors = readField(block, "color");
        if (!(colors instanceof short[][])) {
            return null;
        }
        short[][] rows = (short[][]) colors;
        if (rows.length == 0) {
            return null;
        }
        short[] rgb = rows[meta >= 0 && meta < rows.length ? meta : 0];
        if (rgb == null || rgb.length < 3) {
            return null;
        }
        return new int[] { rgb[0] & 0xFFFF, rgb[1] & 0xFFFF, rgb[2] & 0xFFFF, 255 };
    }

    /**
     * Emit one meta from the block's own texture-name fields, per side when those differ. Returns
     * whether it was handled, so the caller records its gap for everything else.
     */
    private boolean emitNamedTexture(Map<String, Entry> blocks, Block block, String registryName, int meta) {
        int[] tint = namedTextureTint(block, meta);
        NamedIcon[] perSide = new NamedIcon[SIDE_NAMES.length];
        boolean uniform = true;
        for (int side = 0; side < SIDE_NAMES.length; side++) {
            perSide[side] = namedTextureIcon(block, registryName, meta, side);
            if (perSide[side] == null) {
                return false;
            }
            uniform &= perSide[side].iconName.equals(perSide[0].iconName);
        }
        Entry entry = blocks.computeIfAbsent(
            registryName + "|" + meta,
            k -> new Entry("block", null, block.getClass().getName()));
        if (uniform) {
            entry.sides.put("all", singleLayerState(perSide[0], tint));
        } else {
            for (int side = 0; side < SIDE_NAMES.length; side++) {
                entry.sides.put(SIDE_NAMES[side], singleLayerState(perSide[side], tint));
            }
        }
        return true;
    }

    /** The mod domain of a registry name ({@code "bartworks:BW_GlasBlocks"} -> {@code "bartworks"}). */
    private static String registryDomain(String registryName) {
        int colon = registryName.indexOf(':');
        return colon > 0 ? registryName.substring(0, colon) : ICON_DOMAIN;
    }

    /** Resolve a copied casing block's icon (its base layer) via the injected {@code getIcon}. */
    private NamedIcon copiedIcon(Block block, int meta, int copiedSide, int renderSide) {
        int face = copiedSide >= 0 && copiedSide < 6 ? copiedSide : renderSide;
        MethodHandle getIcon = findGetIcon(block);
        if (getIcon == null) {
            return null;
        }
        NamedIcon icon = iconAt(block, getIcon, face, meta);
        if (icon != null) {
            icons.putIfAbsent(icon.iconName, icon.assetPath);
        }
        return icon;
    }

    /** RGBA (r,g,b,a 0-255) from a colour-modulation texture's {@code getRGBA()} / {@code mRGBa}. */
    private int[] rgbaOf(ITexture t) {
        Object rgba = null;
        try {
            rgba = t.getClass().getMethod("getRGBA").invoke(t);
        } catch (Throwable ignored) {
            rgba = readField(t, "mRGBa");
        }
        if (rgba instanceof short[]) {
            short[] s = (short[]) rgba;
            int[] out = { 255, 255, 255, 255 };
            for (int i = 0; i < 4 && i < s.length; i++) {
                out[i] = s[i] & 0xFFFF;
            }
            return out;
        }
        return new int[] { 255, 255, 255, 255 };
    }

    /** Place an MTE at the scratch origin and return the live meta entity bound to its base TE. */
    private IMetaTileEntity place(IMetaTileEntity imte, int id) {
        ItemStack form = imte.getStackForm(1);
        Block block = form != null && form.getItem() != null ? Block.getBlockFromItem(form.getItem()) : null;
        if (block == null || block == Blocks.air) {
            throw new IllegalStateException("no block form");
        }
        world.setBlock(OX, OY, OZ, block, 0, 3);
        TileEntity te = world.getTileEntity(OX, OY, OZ);
        if (!(te instanceof BaseMetaTileEntity)) {
            throw new IllegalStateException("no BaseMetaTileEntity at origin");
        }
        BaseMetaTileEntity bmte = (BaseMetaTileEntity) te;
        bmte.setMetaTileID((short) id);
        IMetaTileEntity mte = imte.newMetaEntity(bmte);
        bmte.setMetaTileEntity(mte);
        mte.setBaseMetaTileEntity(bmte);
        bmte.setFrontFacing(ForgeDirection.NORTH);
        return mte;
    }

    private void wipe() {
        try {
            if (world.getBlock(OX, OY, OZ) != Blocks.air) {
                world.setBlock(OX, OY, OZ, Blocks.air, 0, 2);
            }
        } catch (Throwable ignored) {
            // best-effort cleanup between MTEs
        }
    }

    private static String safeName(IMetaTileEntity imte) {
        try {
            String n = imte.getLocalName();
            if (n != null && !n.trim().isEmpty()) {
                return n;
            }
        } catch (Throwable ignored) {
            // fall through
        }
        return imte.getClass().getSimpleName();
    }

    // ------------------------------------------------------------------------------------------
    // Plain structure blocks (casings, coils, glass) - v1's block-icon mechanism, kept
    // ------------------------------------------------------------------------------------------

    /** Emit a single un-tinted "all"-side layer per meta for every indexed-texture casing/coil block. */
    private int dumpPlainBlocks(Map<String, Entry> blocks) {
        FMLControlledNamespacedRegistry<Block> registry = GameData.getBlockRegistry();
        int stacks = 0;
        for (Object obj : registry) {
            // Every block, not only the GT-indexed ones. The old `instanceof IHasIndexedTexture`
            // filter was a proxy for "a casing/coil a multiblock might place", but it is not one:
            // 75 registry names a dumped multiblock actually uses do not implement it - GT's own
            // gt.blockcasings5 and gt.blockframes among them, plus every bartworks/tectech/kekztech
            // casing and the vanilla blocks some structures include. They were dropped BEFORE the
            // gap branch below, so they went missing silently. Blocks that cannot answer getIcon
            // still cost only a failed lookup, and now say so.
            if (!(obj instanceof Block)) {
                continue;
            }
            Block block = (Block) obj;
            String registryName = String.valueOf(registry.getNameForObject(block));
            // The ITexture accessor is tried FIRST wherever a block has one: it cannot hit the
            // @SideOnly cliff getIcon dies on, and it carries per-layer tint and glow that the
            // single-icon path drops. getIcon stays the fallback for everything else.
            TextureAccessor accessor = findTextureAccessor(block);
            MethodHandle getIcon = findGetIcon(block);
            String getIconError = lastGetIconError;
            // No block-level bail-out on a missing getIcon: a block that cannot answer it may still
            // resolve through the table or through its own texture-name fields, and bailing early is
            // exactly what skipped those routes before they were ever tried.
            for (int meta : realMetas(block, registryName)) {
                if (accessor != null && emitTextureAccessor(blocks, block, accessor, registryName, meta)) {
                    stacks++;
                    continue;
                }
                // A tabled casing family: its getIcon cannot be called server-side at all, so the
                // meta-to-constant mapping comes from the transcribed table instead.
                if (emitTableCasing(blocks, block, registryName, meta)) {
                    stacks++;
                    continue;
                }
                // side 2 (north) as the representative face
                NamedIcon icon = getIcon == null ? null : iconAt(block, getIcon, 2, meta);
                String why = getIcon == null
                    ? "no server-side getIcon override (" + getIconError + ")"
                    : "no icon for meta (" + lastIconError + ")";
                if (icon == null) {
                    // Last resort: the block's own texture-name fields, which survive side-stripping
                    // even where every callable route into its icons has been deleted.
                    if (emitNamedTexture(blocks, block, registryName, meta)) {
                        stacks++;
                        continue;
                    }
                    // Was a bare `continue`, which is how ~37 pairs (the tiered machine casings among
                    // them) went missing with nothing recorded anywhere. lastIconError says whether
                    // getIcon returned null, threw, or handed back an icon this dumper never named.
                    gaps.add(new Gap(registryName, meta, "all", why));
                    continue;
                }
                Entry entry = blocks.computeIfAbsent(
                    registryName + "|" + meta,
                    k -> new Entry("block", null, block.getClass().getName()));
                entry.sides.put("all", singleLayerState(icon));
                stacks++;
            }
        }
        return stacks;
    }

    // ------------------------------------------------------------------------------------------
    // Server-safe ITexture accessors (the preferred plain-block route)
    // ------------------------------------------------------------------------------------------
    //
    // `block.getIcon(side, meta)` is the fragile route, and it fails three different ways on a
    // dedicated server. Some blocks also expose an ITexture accessor that carries NO @SideOnly and
    // never dereferences the icon - it only passes IIconContainers into TextureFactory, which stores
    // them without calling the stripped getIcon(). Those hand back exactly the GTRenderedTexture
    // leaves describe() already reads, so they need no naming logic of their own, AND they carry the
    // per-layer tint and glow flags the single-icon getIcon route throws away. Two shapes exist:
    //
    //   ITexture[][] getTextures(int meta)  one stack per ForgeDirection ordinal  (IBlockWithTextures)
    //   ITexture[]   getTexture(int meta)   one stack shared by all six faces     (BlockFrameBox)
    //
    // Preferred over getIcon wherever present, since it is strictly more information from a route
    // that cannot hit the @SideOnly cliff.

    /**
     * The client-only render meta a coil block adds to reach its lit form ({@code ACTIVE_OFFSET} in
     * {@code BlockCasings5}). It is never stored in world metadata - {@code getClientMeta} synthesizes
     * it - so the inactive stack is dumped at {@code meta} and the active one at {@code meta + 16}.
     */
    private static final int ACTIVE_META_OFFSET = 16;

    /** A block's resolved server-safe ITexture accessor: the handle plus which of the two shapes it is. */
    private static final class TextureAccessor {

        final MethodHandle handle;
        /** {@code getTextures} returns {@code ITexture[][]} (per side); {@code getTexture} one stack. */
        final boolean perSide;

        TextureAccessor(MethodHandle handle, boolean perSide) {
            this.handle = handle;
            this.perSide = perSide;
        }
    }

    /** Resolve {@code getTextures(int)} then {@code getTexture(int)} on {@code block}, or null. */
    private TextureAccessor findTextureAccessor(Block block) {
        MethodHandles.Lookup priv = MethodHandles.lookup();
        for (boolean perSide : new boolean[] { true, false }) {
            String name = perSide ? "getTextures" : "getTexture";
            Class<?> want = perSide ? ITexture[][].class : ITexture[].class;
            for (Class<?> c = block.getClass(); c != null && c != Object.class; c = c.getSuperclass()) {
                try {
                    java.lang.reflect.Method m = c.getDeclaredMethod(name, int.class);
                    if (!want.isAssignableFrom(m.getReturnType())) {
                        break; // a same-named method of another shape; do not keep walking into it
                    }
                    m.setAccessible(true);
                    return new TextureAccessor(priv.unreflect(m), perSide);
                } catch (NoSuchMethodException e) {
                    // keep walking up the hierarchy
                } catch (Throwable t) {
                    break;
                }
            }
        }
        return null;
    }

    /** Side name -> layer stack for one meta via {@code accessor}, or null if it resolved nothing. */
    private Map<String, List<Layer>> textureAccessorLayers(Block block, TextureAccessor accessor, int meta) {
        Object result;
        try {
            result = accessor.handle.invoke(block, meta);
        } catch (Throwable t) {
            lastIconError = (accessor.perSide ? "getTextures" : "getTexture") + " threw "
                + t.getClass().getSimpleName();
            return null;
        }
        Map<String, List<Layer>> out = new TreeMap<>();
        if (accessor.perSide && result instanceof ITexture[][]) {
            ITexture[][] perSide = (ITexture[][]) result;
            for (int side = 0; side < SIDE_NAMES.length && side < perSide.length; side++) {
                if (perSide[side] == null) {
                    continue;
                }
                List<Layer> layers = describeAll(perSide[side], side);
                if (!layers.isEmpty()) {
                    out.put(SIDE_NAMES[side], layers);
                }
            }
        } else if (!accessor.perSide && result instanceof ITexture[]) {
            // One stack for every face: emit it under "all", which the previewer already falls back
            // to, rather than duplicating identical layers across six side keys.
            List<Layer> layers = describeAll((ITexture[]) result, 2);
            if (!layers.isEmpty()) {
                out.put("all", layers);
            }
        }
        if (out.isEmpty()) {
            lastIconError = (accessor.perSide ? "getTextures" : "getTexture") + " resolved no layer";
            return null;
        }
        return out;
    }

    /**
     * Emit one meta through the ITexture accessor, with its active stack when the block has one.
     * Returns whether it was handled, so the caller falls through to {@code getIcon} for the rest.
     *
     * <p>
     * The active probe re-queries at {@code meta + 16} and keeps the result only when it actually
     * differs from the inactive stack, so a block that does not use the coil render-meta convention
     * (every block but {@code BlockCasings5} today) simply contributes no active state rather than
     * mislabelling an unrelated meta's texture as "running".
     */
    private boolean emitTextureAccessor(Map<String, Entry> blocks, Block block, TextureAccessor accessor,
        String registryName, int meta) {
        Map<String, List<Layer>> inactive = textureAccessorLayers(block, accessor, meta);
        if (inactive == null) {
            return false;
        }
        Map<String, List<Layer>> active = accessor.perSide
            ? textureAccessorLayers(block, accessor, meta + ACTIVE_META_OFFSET)
            : null;
        Entry entry = blocks.computeIfAbsent(
            registryName + "|" + meta,
            k -> new Entry("block", null, block.getClass().getName()));
        for (Map.Entry<String, List<Layer>> side : inactive.entrySet()) {
            Map<String, List<Layer>> perState = new TreeMap<>();
            perState.put("inactive", side.getValue());
            List<Layer> lit = active == null ? null : active.get(side.getKey());
            if (lit != null && !sameLayers(lit, side.getValue())) {
                perState.put("active", lit);
            }
            entry.sides.put(side.getKey(), perState);
        }
        return true;
    }

    /** Whether two layer stacks name the same icons with the same tint and glow, in the same order. */
    private static boolean sameLayers(List<Layer> a, List<Layer> b) {
        if (a.size() != b.size()) {
            return false;
        }
        for (int i = 0; i < a.size(); i++) {
            Layer x = a.get(i);
            Layer y = b.get(i);
            if (!x.icon.equals(y.icon) || x.glow != y.glow || !java.util.Arrays.equals(x.rgba, y.rgba)) {
                return false;
            }
        }
        return true;
    }

    // ------------------------------------------------------------------------------------------
    // Shared reflection helpers (from v1)
    // ------------------------------------------------------------------------------------------

    /** Inject a {@link NamedIcon} into every {@code BlockIcons} constant so block.getIcon names it. */
    private void populateIconNames() {
        Field mIconField;
        try {
            mIconField = Textures.BlockIcons.class.getDeclaredField("mIcon");
            mIconField.setAccessible(true);
        } catch (NoSuchFieldException e) {
            throw new IllegalStateException("Textures.BlockIcons.mIcon is gone: " + e.getMessage(), e);
        }
        int ok = 0;
        for (Textures.BlockIcons icon : Textures.BlockIcons.values()) {
            try {
                String rel = "iconsets/" + icon.name();
                mIconField.set(icon, new NamedIcon(ICON_DOMAIN + ":" + rel, assetPath(ICON_DOMAIN, rel)));
                ok++;
            } catch (Throwable t) {
                LOG.debug("gtnh-extractor: cannot name BlockIcons.{}: {}", icon.name(), t.toString());
            }
        }
        LOG.info("gtnh-extractor: named {} BlockIcons constants", ok);
        injectQueuedIconContainers();
    }

    /**
     * Inject a {@link NamedIcon} into every custom icon CONTAINER GT queued for client-side icon
     * registration, so the blocks holding them answer {@code getIcon} on the server too.
     *
     * <p>
     * {@link #populateIconNames} covers the {@code BlockIcons} <b>enum constants</b>. It cannot reach
     * the container <b>classes</b> - GT++'s {@code TexturesGtBlock.CustomIcon} (315 statics), GT's own
     * {@code Textures.BlockIcons.CustomIcon}, and every addon's holder - whose {@code mIcon} field is
     * assigned only inside their {@code run()}, and the only thing that drains those Runnables is
     * {@code BlockMachines.registerBlockIcons}, which is {@code @SideOnly(CLIENT)}. Server-side the
     * queue is fully populated and simply never run, which is why those blocks answer {@code getIcon}
     * with <i>null</i> rather than throwing - the "getIcon returned null" gap reason.
     *
     * <p>
     * Every such container self-registers into {@code GregTechAPI.sGTBlockIconload} from its own
     * constructor, so that public list <b>is</b> the complete server-side registry of them. Walking it
     * beats enumerating any single holder class's fields: it also catches instances kept in private
     * statics elsewhere (kekztech's, and {@code TexturesGrinderMultiblock}'s 18) and any mod's holder
     * we have never heard of, with no per-class knowledge and no hand-maintained list.
     */
    private void injectQueuedIconContainers() {
        List<Runnable> queue = GregTechAPI.sGTBlockIconload;
        if (queue == null) {
            LOG.warn("gtnh-extractor: GregTechAPI.sGTBlockIconload is null; custom icon containers stay unnamed");
            return;
        }
        int ok = 0;
        int skipped = 0;
        for (Runnable r : new ArrayList<>(queue)) {
            if (r == null || r instanceof Enum) {
                continue; // enum constants are already named by populateIconNames
            }
            String[] ref = iconRef(r);
            if (ref == null) {
                skipped++;
                continue;
            }
            if (writeField(r, "mIcon", new NamedIcon(ref[0] + ":" + ref[1], assetPath(ref[0], ref[1])))) {
                ok++;
            } else {
                skipped++;
            }
        }
        LOG.info("gtnh-extractor: named {} queued icon containers ({} unnameable)", ok, skipped);
    }

    /**
     * Per-family {@code meta -> Textures.BlockIcons constant} tables for the casing blocks whose own
     * {@code getIcon} cannot be called on a dedicated server, transcribed from GT source at the
     * pinned version.
     *
     * <p>
     * <b>Why a table at all.</b> These families fail two different ways, neither recoverable by
     * reflection. Most declare {@code getIcon(int,int)} {@code @SideOnly(CLIENT)}, so FML's
     * SideTransformer <i>deletes the method</i> and there is nothing left to call. The rest survive
     * but reach the sprite through {@code invokeinterface IIconContainer.getIcon()}, and that
     * interface method IS {@code @SideOnly}, so the call dies with NoSuchMethodError. Either way the
     * meta-to-constant mapping exists only in a method body we cannot execute, and the constants
     * themselves are perfectly readable static fields - so we name them directly and never touch the
     * stripped method.
     *
     * <p>
     * <b>Shape of a value.</b> One element means one constant on all six faces; three mean
     * {@code [DOWN, UP, SIDE]}, for the families that texture their bottom and top differently. A
     * constant prefixed {@link #ARRAY_MARKER} names a {@code BlockIcons} <i>array</i> field to be
     * indexed by the meta rather than a constant - the tiered {@code MACHINECASINGS_*} arrays, whose
     * 45 elements are read at runtime rather than transcribed, so a GT tier addition cannot silently
     * shift them.
     *
     * <p>
     * <b>This is an allowlist on purpose.</b> It would be easy to generalise to "any block with a
     * stripped getIcon", and that would be actively harmful: {@code BlockMachines.getIcon} is a
     * vestigial stub returning {@code MACHINE_LV_SIDE} for <i>every</i> meta, so a generic rule would
     * confidently skin every GT machine hull as an LV casing side. A plausible wrong sprite is worse
     * than a grey one, because nothing flags it. A family not listed here keeps its recorded gap.
     *
     * <p>
     * <b>The maintenance hazard, stated plainly.</b> A GT5U bump that inserts a meta shifts every
     * later entry in that family, and the result still validates and still renders - it is just
     * wrong. {@link #verifyCasingTable} exists to catch exactly that: it checks every constant named
     * here still resolves, and logs loudly when one does not.
     */
    // A LinkedHashMap rather than Map.of: this targets Java 8 bytecode (Jabel gives modern syntax,
    // not the modern stdlib), so the Java 9 factory methods are not on the classpath here.
    private static final Map<String, Map<Integer, String[]>> CASING_ICON_TABLE = new LinkedHashMap<>();

    /** Prefix marking a table entry as a {@code BlockIcons} array field indexed by the meta. */
    private static final String ARRAY_MARKER = "[";

    /** Record {@code meta} of {@code registryName} as one constant shown on all six faces. */
    private static void flat(String registryName, int meta, String constant) {
        CASING_ICON_TABLE.computeIfAbsent(registryName, k -> new LinkedHashMap<>())
            .put(meta, new String[] { constant });
    }

    /** Record {@code meta} of {@code registryName} as distinct bottom / top / wall constants. */
    private static void sided(String registryName, int meta, String down, String up, String side) {
        CASING_ICON_TABLE.computeIfAbsent(registryName, k -> new LinkedHashMap<>())
            .put(meta, new String[] { down, up, side });
    }

    private static final String TIER_BOTTOM = ARRAY_MARKER + "MACHINECASINGS_BOTTOM";
    private static final String TIER_TOP = ARRAY_MARKER + "MACHINECASINGS_TOP";
    private static final String TIER_SIDE = ARRAY_MARKER + "MACHINECASINGS_SIDE";

    static {
        // gt.blockcasings - the tiered machine casings. Metas 10-15 always worked through getIcon;
        // 0-9 never did, which is why an ExxonMobil Chemical Plant rendered mostly grey.
        for (int meta = 0; meta < 15; meta++) {
            sided("gregtech:gt.blockcasings", meta, TIER_BOTTOM, TIER_TOP, TIER_SIDE);
        }

        // gt.blockcasings6 - tiered bottom/top over a per-meta tank wall. Meta 0 reaches the wall
        // constant through the switch `default` arm, so it is a real mapping, not a fallback.
        for (int meta = 0; meta < 15; meta++) {
            sided("gregtech:gt.blockcasings6", meta, TIER_BOTTOM, TIER_TOP, "MACHINE_CASING_TANK_" + meta);
        }

        // gt.blockcasings8 - the Large Chemical Reactor's family (metas 0 and 1). Meta 9 has a
        // switch arm but is never registered, so it is deliberately absent here.
        flat("gregtech:gt.blockcasings8", 0, "MACHINE_CASING_CHEMICALLY_INERT");
        flat("gregtech:gt.blockcasings8", 1, "MACHINE_CASING_PIPE_POLYTETRAFLUOROETHYLENE");
        flat("gregtech:gt.blockcasings8", 2, "MACHINE_CASING_MINING_NEUTRONIUM");
        flat("gregtech:gt.blockcasings8", 3, "MACHINE_CASING_MINING_BLACKPLUTONIUM");
        flat("gregtech:gt.blockcasings8", 4, "MACHINE_CASING_EXTREME_ENGINE_INTAKE");
        flat("gregtech:gt.blockcasings8", 5, "MACHINE_CASING_ADVANCEDRADIATIONPROOF");
        flat("gregtech:gt.blockcasings8", 6, "MACHINE_CASING_RHODIUM_PALLADIUM");
        flat("gregtech:gt.blockcasings8", 7, "MACHINE_CASING_IRIDIUM");
        flat("gregtech:gt.blockcasings8", 8, "MACHINE_CASING_MAGICAL");
        flat("gregtech:gt.blockcasings8", 10, "MACHINE_CASING_RADIANT_NAQUADAH_ALLOY");
        flat("gregtech:gt.blockcasings8", 11, "MACHINE_CASING_PCB_TIER_1");
        flat("gregtech:gt.blockcasings8", 12, "MACHINE_CASING_PCB_TIER_2");
        flat("gregtech:gt.blockcasings8", 13, "MACHINE_CASING_PCB_TIER_3");
        flat("gregtech:gt.blockcasings8", 14, "INFINITY_COOLED_CASING");

        // gt.blockcasings9 - flat except meta 2, whose bottom/top differ from its walls.
        flat("gregtech:gt.blockcasings9", 0, "MACHINE_CASING_PIPE_POLYBENZIMIDAZOLE");
        flat("gregtech:gt.blockcasings9", 1, "MACHINE_CASING_VENT_T2");
        sided("gregtech:gt.blockcasings9", 2, "TEXTURE_METAL_PANEL_E_A", "TEXTURE_METAL_PANEL_E_A",
            "TEXTURE_METAL_PANEL_E");
        flat("gregtech:gt.blockcasings9", 3, "INDUSTRIAL_STRENGTH_CONCRETE");
        flat("gregtech:gt.blockcasings9", 4, "MACHINE_CASING_INDUSTRIAL_WATER_PLANT");
        flat("gregtech:gt.blockcasings9", 5, "WATER_PLANT_CONCRETE_CASING");
        flat("gregtech:gt.blockcasings9", 6, "MACHINE_CASING_FLOCCULATION");
        flat("gregtech:gt.blockcasings9", 7, "MACHINE_CASING_NAQUADAH_REINFORCED_WATER_PLANT");
        flat("gregtech:gt.blockcasings9", 8, "MACHINE_CASING_EXTREME_CORROSION_RESISTANT");
        flat("gregtech:gt.blockcasings9", 9, "MACHINE_CASING_HIGH_PRESSURE_RESISTANT");
        flat("gregtech:gt.blockcasings9", 10, "MACHINE_CASING_OZONE");
        flat("gregtech:gt.blockcasings9", 11, "MACHINE_CASING_PLASMA_HEATER");
        flat("gregtech:gt.blockcasings9", 12, "NAQUADRIA_REINFORCED_WATER_PLANT_CASING");
        flat("gregtech:gt.blockcasings9", 13, "UV_BACKLIGHT_STERILIZER_CASING");
        flat("gregtech:gt.blockcasings9", 14, "BLOCK_QUARK_PIPE");
        flat("gregtech:gt.blockcasings9", 15, "BLOCK_QUARK_RELEASE_CHAMBER");

        // gt.blockcasings10 - flat except meta 15 (reinforced wood, distinct top/bottom).
        flat("gregtech:gt.blockcasings10", 0, "MACHINE_CASING_EMS");
        flat("gregtech:gt.blockcasings10", 1, "MACHINE_CASING_LASER");
        flat("gregtech:gt.blockcasings10", 2, "BLOCK_QUARK_CONTAINMENT_CASING");
        flat("gregtech:gt.blockcasings10", 3, "MACHINE_CASING_AUTOCLAVE");
        flat("gregtech:gt.blockcasings10", 4, "COMPRESSOR_CASING");
        flat("gregtech:gt.blockcasings10", 5, "COMPRESSOR_PIPE_CASING");
        flat("gregtech:gt.blockcasings10", 6, "NEUTRONIUM_CASING");
        flat("gregtech:gt.blockcasings10", 7, "NEUTRONIUM_ACTIVE_CASING");
        flat("gregtech:gt.blockcasings10", 8, "NEUTRONIUM_STABLE_CASING");
        flat("gregtech:gt.blockcasings10", 9, "COOLANT_DUCT_CASING");
        flat("gregtech:gt.blockcasings10", 10, "HEATING_DUCT_CASING");
        flat("gregtech:gt.blockcasings10", 11, "EXTREME_DENSITY_CASING");
        flat("gregtech:gt.blockcasings10", 12, "RADIATION_ABSORBENT_CASING");
        flat("gregtech:gt.blockcasings10", 13, "MACHINE_CASING_MS160");
        flat("gregtech:gt.blockcasings10", 14, "RADIATOR_MS160");
        sided("gregtech:gt.blockcasings10", 15, "CASING_REINFORCED_WOOD_TOP", "CASING_REINFORCED_WOOD_TOP",
            "CASING_REINFORCED_WOOD");

        // gt.blockcasings11 - item pipe casings. Meta 0 reaches TIN through the `default` arm.
        flat("gregtech:gt.blockcasings11", 0, "MACHINE_CASING_ITEM_PIPE_TIN");
        flat("gregtech:gt.blockcasings11", 1, "MACHINE_CASING_ITEM_PIPE_BRASS");
        flat("gregtech:gt.blockcasings11", 2, "MACHINE_CASING_ITEM_PIPE_ELECTRUM");
        flat("gregtech:gt.blockcasings11", 3, "MACHINE_CASING_ITEM_PIPE_PLATINUM");
        flat("gregtech:gt.blockcasings11", 4, "MACHINE_CASING_ITEM_PIPE_OSMIUM");
        flat("gregtech:gt.blockcasings11", 5, "MACHINE_CASING_ITEM_PIPE_QUANTIUM");
        flat("gregtech:gt.blockcasings11", 6, "MACHINE_CASING_ITEM_PIPE_FLUXED_ELECTRUM");
        flat("gregtech:gt.blockcasings11", 7, "MACHINE_CASING_ITEM_PIPE_BLACK_PLUTONIUM");

        // gt.blockcasings12 - starts at meta 10; 0-9 are unregistered.
        flat("gregtech:gt.blockcasings12", 10, "MACHINE_CASING_THAUMIUM");
        flat("gregtech:gt.blockcasings12", 11, "MACHINE_CASING_VOID");
        flat("gregtech:gt.blockcasings12", 12, "MACHINE_CASING_ICHORIUM");

        // gt.blockcasings13 - constant names are offset from the metas and unrelated to item names.
        flat("gregtech:gt.blockcasings13", 5, "NANO_FORGE_CASING_1");
        flat("gregtech:gt.blockcasings13", 6, "NANO_FORGE_CASING_2");
        flat("gregtech:gt.blockcasings13", 7, "NANO_FORGE_CASING_3");
        flat("gregtech:gt.blockcasings13", 8, "NANO_FORGE_CASING_4");
        flat("gregtech:gt.blockcasings13", 9, "NANITE_CORE");

        // gt.blockcasingsNH - metas 0-6 flat, 10-14 tiered arrays (the per-side logic lives inside
        // this family's `default` arm, so it applies only above meta 6). Meta 2 really does use the
        // tiered MACHINE_ULV_SIDE constant as a flat casing; that is not a transcription slip.
        flat("gregtech:gt.blockcasingsNH", 0, "MACHINE_CASING_TURBINE_STEEL");
        flat("gregtech:gt.blockcasingsNH", 1, "MACHINE_CASING_PIPE_STEEL");
        flat("gregtech:gt.blockcasingsNH", 2, "MACHINE_ULV_SIDE");
        flat("gregtech:gt.blockcasingsNH", 3, "MACHINE_CASING_STABLE_TITANIUM");
        flat("gregtech:gt.blockcasingsNH", 4, "MACHINE_CASING_PIPE_TITANIUM");
        flat("gregtech:gt.blockcasingsNH", 5, "MACHINE_CASING_ROBUST_TUNGSTENSTEEL");
        flat("gregtech:gt.blockcasingsNH", 6, "MACHINE_CASING_PIPE_TUNGSTENSTEEL");
        for (int meta = 10; meta < 15; meta++) {
            sided("gregtech:gt.blockcasingsNH", meta, TIER_BOTTOM, TIER_TOP, TIER_SIDE);
        }

        // gt.blocktintedglass - no String field and no callable getIcon, so the table is the only
        // route left. Its four metas are plain BlockIcons constants, verified by verifyCasingTable.
        flat("gregtech:gt.blocktintedglass", 0, "GLASS_TINTED_INDUSTRIAL_WHITE");
        flat("gregtech:gt.blocktintedglass", 1, "GLASS_TINTED_INDUSTRIAL_LIGHT_GRAY");
        flat("gregtech:gt.blocktintedglass", 2, "GLASS_TINTED_INDUSTRIAL_GRAY");
        flat("gregtech:gt.blocktintedglass", 3, "GLASS_TINTED_INDUSTRIAL_BLACK");

        // gt.blockglass1 - meta 5's constant says FRAME though the item is "Nanite Shielding Glass".
        flat("gregtech:gt.blockglass1", 0, "GLASS_PH_RESISTANT");
        flat("gregtech:gt.blockglass1", 1, "NEUTRONIUM_COATED_UV_RESISTANT_GLASS");
        flat("gregtech:gt.blockglass1", 2, "OMNI_PURPOSE_INFINITY_FUSED_GLASS");
        flat("gregtech:gt.blockglass1", 3, "GLASS_QUARK_CONTAINMENT");
        flat("gregtech:gt.blockglass1", 4, "HAWKING_GLASS");
        flat("gregtech:gt.blockglass1", 5, "NANITE_SHIELDING_FRAME");
    }

    /**
     * Check every constant the table names still resolves, and log the ones that do not.
     *
     * <p>
     * This is the guard against the table's one real failure mode. A hand-transcribed mapping cannot
     * detect that GT renamed or removed a constant - the dump just emits a wrong or missing sprite
     * and everything downstream believes it. Resolving each name once at startup turns a silent
     * regression after a GT5U bump into a loud line in the run log.
     */
    private void verifyCasingTable() {
        List<String> broken = new ArrayList<>();
        for (Map.Entry<String, Map<Integer, String[]>> family : CASING_ICON_TABLE.entrySet()) {
            for (Map.Entry<Integer, String[]> entry : family.getValue().entrySet()) {
                for (String constant : entry.getValue()) {
                    if (tableIcon(constant, entry.getKey()) == null) {
                        broken.add(family.getKey() + "|" + entry.getKey() + " -> " + constant);
                    }
                }
            }
        }
        if (broken.isEmpty()) {
            LOG.info("gtnh-extractor: casing icon table verified ({} families)", CASING_ICON_TABLE.size());
        } else {
            LOG.error(
                "gtnh-extractor: {} casing table entries no longer resolve - GT constants moved, "
                    + "the table needs re-transcribing: {}",
                broken.size(),
                broken);
        }
    }

    /**
     * Emit one tabled casing meta. Returns whether it was handled, so the caller falls through to
     * the {@code getIcon} path (and its gap) for everything else.
     */
    private boolean emitTableCasing(Map<String, Entry> blocks, Block block, String registryName, int meta) {
        Map<Integer, String[]> family = CASING_ICON_TABLE.get(registryName);
        if (family == null) {
            return false;
        }
        String[] spec = family.get(meta);
        if (spec == null) {
            return false;
        }
        Map<String, Map<String, List<Layer>>> sides = new TreeMap<>();
        if (spec.length == 1) {
            // One sprite everywhere: emit the single "all" side the previewer already falls back to,
            // rather than six identical entries.
            NamedIcon icon = tableIcon(spec[0], meta);
            if (icon == null) {
                return false;
            }
            sides.put("all", singleLayerState(icon));
        } else {
            for (int side = 0; side < SIDE_NAMES.length; side++) {
                NamedIcon icon = tableIcon(spec[side == 0 ? 0 : side == 1 ? 1 : 2], meta);
                if (icon == null) {
                    return false; // a half-resolvable meta is not worth half-emitting; keep the gap
                }
                sides.put(SIDE_NAMES[side], singleLayerState(icon));
            }
        }
        Entry entry = blocks.computeIfAbsent(
            registryName + "|" + meta,
            k -> new Entry("block", null, block.getClass().getName()));
        entry.sides.putAll(sides);
        return true;
    }

    /** A one-layer, un-tinted {@code {"inactive": [icon]}} state, registering the icon's jar path. */
    private Map<String, List<Layer>> singleLayerState(NamedIcon icon) {
        return singleLayerState(icon, null);
    }

    /** As {@link #singleLayerState(NamedIcon)}, with an optional RGBA multiply ({@code null} = none). */
    private Map<String, List<Layer>> singleLayerState(NamedIcon icon, int[] tint) {
        icons.putIfAbsent(icon.iconName, icon.assetPath);
        Map<String, List<Layer>> perState = new TreeMap<>();
        List<Layer> layers = new ArrayList<>();
        layers.add(new Layer(icon.iconName, tint == null ? new int[] { 255, 255, 255, 255 } : tint, false));
        perState.put("inactive", layers);
        return perState;
    }

    /**
     * Resolve one table entry to its icon: a {@code BlockIcons} constant by name, or - behind
     * {@link #ARRAY_MARKER} - the {@code meta}-th element of a {@code BlockIcons} array field.
     * {@code null} if the field is gone or holds something unexpected, which
     * {@link #verifyCasingTable} reports and the caller records as a gap.
     */
    private NamedIcon tableIcon(String constant, int meta) {
        boolean indexed = constant.startsWith(ARRAY_MARKER);
        String fieldName = indexed ? constant.substring(ARRAY_MARKER.length()) : constant;
        try {
            Field field = Textures.BlockIcons.class.getDeclaredField(fieldName);
            field.setAccessible(true);
            Object value = field.get(null);
            if (indexed) {
                if (value == null || meta < 0 || meta >= java.lang.reflect.Array.getLength(value)) {
                    return null;
                }
                value = java.lang.reflect.Array.get(value, meta);
            }
            if (!(value instanceof Textures.BlockIcons)) {
                return null;
            }
            String rel = "iconsets/" + ((Textures.BlockIcons) value).name();
            return new NamedIcon(ICON_DOMAIN + ":" + rel, assetPath(ICON_DOMAIN, rel));
        } catch (Throwable t) {
            LOG.debug("gtnh-extractor: casing table lookup failed for {}|{}: {}", constant, meta, t.toString());
            return null;
        }
    }

    private NamedIcon iconAt(Block block, MethodHandle getIcon, int side, int meta) {
        try {
            Object icon = getIcon.invoke(block, side, meta);
            if (icon instanceof NamedIcon) {
                return (NamedIcon) icon;
            }
            lastIconError = icon == null ? "getIcon returned null" : "getIcon returned a foreign icon";
        } catch (Throwable t) {
            lastIconError = t.getClass().getSimpleName();
        }
        return null;
    }

    private MethodHandle findGetIcon(Block block) {
        MethodType type = MethodType.methodType(IIcon.class, int.class, int.class);
        MethodHandles.Lookup pub = MethodHandles.publicLookup();
        MethodHandles.Lookup priv = MethodHandles.lookup();
        for (String name : GET_ICON_NAMES) {
            // 1. Targeted public virtual resolution (links one invokevirtual, never touches the
            // client-only registerBlockIcons sibling a bulk getMethods() scan would trip over).
            try {
                return pub.findVirtual(block.getClass(), name, type);
            } catch (NoSuchMethodException | IllegalAccessException e) {
                lastGetIconError = name + ": " + e.getClass().getSimpleName();
            } catch (Throwable t) {
                lastGetIconError = name + ": " + t.getClass().getSimpleName() + " " + t.getMessage();
            }
            // 2. Fallback: unreflect the declared getIcon(int,int) walking the hierarchy. Some casings
            // (e.g. the newer families) declare an un-annotated getIcon that publicLookup does not bind
            // but a direct getDeclaredMethod + unreflect does; still targeted, so no sibling load.
            for (Class<?> c = block.getClass(); c != null && c != Object.class; c = c.getSuperclass()) {
                try {
                    java.lang.reflect.Method m = c.getDeclaredMethod(name, int.class, int.class);
                    m.setAccessible(true);
                    return priv.unreflect(m);
                } catch (NoSuchMethodException e) {
                    // keep walking up the hierarchy
                } catch (Throwable t) {
                    lastGetIconError = name + " unreflect: " + t.getClass().getSimpleName();
                }
            }
        }
        return null;
    }

    /** Why the most recent {@link #findGetIcon} returned null, folded into the block-level gap. */
    private String lastGetIconError;

    /**
     * Registry names whose sub-blocks are keyed by GT <b>material id</b> (an index into
     * {@code GregTechAPI.sGeneratedMaterials}, up to 1000) rather than by world block metadata.
     *
     * <p>
     * An allowlist rather than a blanket wide scan, for the same reason
     * {@link #CASING_ICON_TABLE} is one. A block's meta space is only as wide as its own indexing,
     * and the display-name test is not a reliable bound above 15: scanning every block to 1000
     * emitted <b>876 metas for the coil block</b>, whose {@code getTextures} answers any meta at all
     * through its {@code default} arm - 862 confident, wrong, Cupronickel-skinned entries the
     * previewer would have trusted. Widening only where the indexing genuinely calls for it keeps
     * that impossible.
     */
    private static final TreeSet<String> MATERIAL_INDEXED_BLOCKS = new TreeSet<>();

    static {
        MATERIAL_INDEXED_BLOCKS.add("gregtech:gt.blockframes");
    }

    /**
     * The block's real sub-block metas (mirrors GT's creative-list test: an unnamed meta's display
     * name still contains {@code ".name"}). Falls back to the full 0..15 range if names are absent.
     *
     * <p>
     * The range is 0..15 - world block metadata - except for the {@link #MATERIAL_INDEXED_BLOCKS},
     * which key their sub-blocks by an index into {@code GregTechAPI.sGeneratedMaterials} instead, so
     * a dumped structure legitimately references {@code gt.blockframes|316}. Scanning only 0..15 left
     * every one of those unresolved, which is why frames stayed grey in 50 of 208 dumped multiblocks
     * even once their ITexture accessor worked.
     */
    private int[] realMetas(Block block, String registryName) {
        Item item = Item.getItemFromBlock(block);
        TreeSet<Integer> metas = new TreeSet<>();
        if (item != null) {
            int limit = MATERIAL_INDEXED_BLOCKS.contains(registryName) ? GregTechAPI.sGeneratedMaterials.length : 16;
            for (int meta = 0; meta < limit; meta++) {
                try {
                    String name = new ItemStack(item, 1, meta).getDisplayName();
                    if (name != null && !name.contains(".name")) {
                        metas.add(meta);
                    }
                } catch (Throwable ignored) {
                    // not a real sub-block
                }
            }
        }
        if (metas.isEmpty()) {
            for (int meta = 0; meta < 16; meta++) {
                metas.add(meta);
            }
        }
        int[] out = new int[metas.size()];
        int i = 0;
        for (int meta : metas) {
            out[i++] = meta;
        }
        return out;
    }

    // ------------------------------------------------------------------------------------------
    // Small reflection utilities
    // ------------------------------------------------------------------------------------------

    private static Object readField(Object owner, String name) {
        for (Class<?> c = owner.getClass(); c != null && c != Object.class; c = c.getSuperclass()) {
            try {
                Field f = c.getDeclaredField(name);
                f.setAccessible(true);
                return f.get(owner);
            } catch (NoSuchFieldException e) {
                // keep walking up
            } catch (Throwable t) {
                return null;
            }
        }
        return null;
    }

    /** Set {@code owner.name = value}, walking up the hierarchy; whether the write landed. */
    private static boolean writeField(Object owner, String name, Object value) {
        for (Class<?> c = owner.getClass(); c != null && c != Object.class; c = c.getSuperclass()) {
            try {
                Field f = c.getDeclaredField(name);
                f.setAccessible(true);
                f.set(owner, value);
                return true;
            } catch (NoSuchFieldException e) {
                // keep walking up
            } catch (Throwable t) {
                return false;
            }
        }
        return false;
    }

    private static boolean boolField(Object owner, String name) {
        Object v = readField(owner, name);
        return v instanceof Boolean && (Boolean) v;
    }

    private static int intField(Object owner, String name) {
        Object v = readField(owner, name);
        return v instanceof Number ? ((Number) v).intValue() : -1;
    }

    // ------------------------------------------------------------------------------------------
    // Manifest writer (schema 2)
    // ------------------------------------------------------------------------------------------

    private void writeManifest(File file, String packVersion, Map<String, String> modVersions, String extractorSha,
        Map<String, Entry> blocks) throws IOException {
        JsonObject root = new JsonObject();
        root.addProperty("schema", SCHEMA_VERSION);
        root.addProperty("method", "server-itexture-reflection");

        JsonObject provenance = new JsonObject();
        provenance.addProperty("pack_version", packVersion);
        JsonObject mods = new JsonObject();
        modVersions.forEach(mods::addProperty);
        provenance.add("mod_versions", mods);
        provenance.addProperty("generated_at", java.time.Instant.now().toString());
        provenance.addProperty("extractor_sha", extractorSha);
        provenance.addProperty(
            "note",
            "Layered ITexture stacks reflected server-side (icon name + rgba tint + glow) per side "
                + "and active state. PNGs are NOT committed (LGPL); fetch from the GT5-Unofficial jar "
                + "using the paths in `icons` and composite per `blocks` (see previewer/bake.py).");
        JsonObject coverage = new JsonObject();
        long mteCount = blocks.values().stream().filter(e -> "mte".equals(e.kind)).count();
        coverage.addProperty("blocks", blocks.size());
        coverage.addProperty("mte", (int) mteCount);
        coverage.addProperty("icons", icons.size());
        coverage.addProperty("gaps", gaps.size());
        provenance.add("coverage", coverage);
        root.add("provenance", provenance);

        root.addProperty("asset_root", "assets/{modid}/textures/blocks/");

        JsonObject blocksJson = new JsonObject();
        for (Map.Entry<String, Entry> e : blocks.entrySet()) {
            Entry entry = e.getValue();
            JsonObject bj = new JsonObject();
            bj.addProperty("kind", entry.kind);
            if (entry.displayName != null) {
                bj.addProperty("display_name", entry.displayName);
            }
            if (entry.sourceClass != null) {
                bj.addProperty("source_class", entry.sourceClass);
            }
            JsonObject sidesJson = new JsonObject();
            for (Map.Entry<String, Map<String, List<Layer>>> side : entry.sides.entrySet()) {
                JsonObject statesJson = new JsonObject();
                for (Map.Entry<String, List<Layer>> state : side.getValue().entrySet()) {
                    statesJson.add(state.getKey(), layersJson(state.getValue()));
                }
                sidesJson.add(side.getKey(), statesJson);
            }
            bj.add("sides", sidesJson);
            blocksJson.add(e.getKey(), bj);
        }
        root.add("blocks", blocksJson);

        JsonObject iconsJson = new JsonObject();
        icons.forEach(iconsJson::addProperty);
        root.add("icons", iconsJson);

        JsonArray gapsJson = new JsonArray();
        gaps.stream()
            .sorted(
                java.util.Comparator.comparing((Gap g) -> g.block).thenComparingInt(g -> g.meta)
                    .thenComparing(g -> g.side))
            .forEach(g -> {
                JsonObject gj = new JsonObject();
                gj.addProperty("block", g.block);
                gj.addProperty("meta", g.meta);
                gj.addProperty("side", g.side);
                gj.addProperty("reason", g.reason);
                gapsJson.add(gj);
            });
        root.add("gaps", gapsJson);

        file.getParentFile().mkdirs();
        try (Writer w = Files.newBufferedWriter(file.toPath(), StandardCharsets.UTF_8)) {
            gson.toJson(root, w);
            w.write('\n');
        }
    }

    private static JsonArray layersJson(List<Layer> layers) {
        JsonArray arr = new JsonArray();
        for (Layer l : layers) {
            JsonObject lj = new JsonObject();
            lj.addProperty("icon", l.icon);
            JsonArray rgba = new JsonArray();
            for (int c : l.rgba) {
                rgba.add(new JsonPrimitive(c));
            }
            lj.add("rgba", rgba);
            lj.addProperty("glow", l.glow);
            arr.add(lj);
        }
        return arr;
    }

    /**
     * A server-safe {@link IIcon} carrying only a registered name and jar path, injected into the
     * {@code BlockIcons.mIcon} fields so a block's own {@code getIcon} hands it back on the server.
     */
    private static final class NamedIcon implements IIcon {

        final String iconName;
        final String assetPath;

        NamedIcon(String iconName, String assetPath) {
            this.iconName = iconName;
            this.assetPath = assetPath;
        }

        @Override
        public int getIconWidth() {
            return 16;
        }

        @Override
        public int getIconHeight() {
            return 16;
        }

        @Override
        public float getMinU() {
            return 0.0f;
        }

        @Override
        public float getMaxU() {
            return 1.0f;
        }

        @Override
        public float getInterpolatedU(double u) {
            return 0.0f;
        }

        @Override
        public float getMinV() {
            return 0.0f;
        }

        @Override
        public float getMaxV() {
            return 1.0f;
        }

        @Override
        public float getInterpolatedV(double v) {
            return 0.0f;
        }

        @Override
        public String getIconName() {
            return iconName;
        }
    }
}
