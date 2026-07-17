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
import gregtech.api.interfaces.IHasIndexedTexture;
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
 * {@code ITexture[]} is obtained server-side - via the {@code getXxxFacing{Inactive,Active}(byte)}
 * accessors for basic single-block machines (reliable, no tile entity), and via
 * {@code getTexture(base, side, facing, colour, active, redstone)} for the rest (placed like the
 * StructureDumper does). Each layer is a {@code GTRenderedTexture} ({@code mIconContainer} +
 * {@code getRGBA()} + {@code glow}), a sided/multi wrapper (recursed via {@code mTextures}), or a
 * {@code GTCopiedBlockTextureRender} whose copied casing icon is resolved via the block-icon path.
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
        }
        return stacks;
    }

    /**
     * A basic single-block machine's layer stack for one side/state, via the {@code getXxxFacing}
     * accessors (no base tile entity needed - the spike showed {@code getTexture} NPEs on a bare
     * placement for some of these). Front maps to NORTH; the other horizontals share the side texture.
     */
    private List<Layer> basicMachineLayers(MTEBasicMachine mte, int side, boolean active) {
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
            // Colour index -1 = unpainted, so the base casing keeps the default MACHINE_METAL steel
            // tint. A real index (0..15) paints it that dye (0 = black), which greyed every machine.
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
        // Unknown ITexture implementation (exotic ISBRH renderer): record it, do not guess.
        gaps.add(new Gap(t.getClass().getName(), -1, SIDE_NAMES[renderSide], "unknown ITexture class"));
    }

    /** An {@code IIconContainer}'s iconset name: enum {@code name()}, else a custom {@code mIconName}. */
    private String iconName(IIconContainer c) {
        String rel = null;
        if (c instanceof Enum) {
            rel = "iconsets/" + ((Enum<?>) c).name();
        } else {
            Object mIconName = readField(c, "mIconName");
            if (mIconName instanceof String && !((String) mIconName).isEmpty()) {
                rel = (String) mIconName;
            }
        }
        if (rel == null) {
            gaps.add(new Gap(c.getClass().getName(), -1, "all", "unresolvable IIconContainer"));
            return null;
        }
        String name = ICON_DOMAIN + ":" + rel;
        icons.putIfAbsent(name, "assets/" + ICON_DOMAIN + "/textures/blocks/" + rel + ".png");
        return name;
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
            if (!(obj instanceof IHasIndexedTexture) || !(obj instanceof Block)) {
                continue;
            }
            Block block = (Block) obj;
            String registryName = String.valueOf(registry.getNameForObject(block));
            MethodHandle getIcon = findGetIcon(block);
            if (getIcon == null) {
                gaps.add(new Gap(registryName, -1, "all",
                    "no server-side getIcon override (" + lastGetIconError + ")"));
                continue;
            }
            for (int meta : realMetas(block)) {
                NamedIcon icon = iconAt(block, getIcon, 2, meta); // side 2 (north) as the representative face
                if (icon == null) {
                    continue;
                }
                icons.putIfAbsent(icon.iconName, icon.assetPath);
                String key = registryName + "|" + meta;
                Entry entry = blocks.computeIfAbsent(key, k -> new Entry("block", null, block.getClass().getName()));
                Map<String, List<Layer>> perState = new TreeMap<>();
                List<Layer> layers = new ArrayList<>();
                layers.add(new Layer(icon.iconName, new int[] { 255, 255, 255, 255 }, false));
                perState.put("inactive", layers);
                entry.sides.put("all", perState);
                stacks++;
            }
        }
        return stacks;
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
                String shortName = "iconsets/" + icon.name();
                String name = ICON_DOMAIN + ":" + shortName;
                String path = "assets/" + ICON_DOMAIN + "/textures/blocks/" + shortName + ".png";
                mIconField.set(icon, new NamedIcon(name, path));
                ok++;
            } catch (Throwable t) {
                LOG.debug("gtnh-extractor: cannot name BlockIcons.{}: {}", icon.name(), t.toString());
            }
        }
        LOG.info("gtnh-extractor: named {} BlockIcons constants", ok);
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
     * The block's real sub-block metas (mirrors GT's creative-list test: an unnamed meta's display
     * name still contains {@code ".name"}). Falls back to the full 0..15 range if names are absent.
     */
    private int[] realMetas(Block block) {
        Item item = Item.getItemFromBlock(block);
        TreeSet<Integer> metas = new TreeSet<>();
        if (item != null) {
            for (int meta = 0; meta < 16; meta++) {
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
