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
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.TreeSet;

import net.minecraft.block.Block;
import net.minecraft.item.Item;
import net.minecraft.item.ItemStack;
import net.minecraft.util.IIcon;

import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;

import cpw.mods.fml.common.registry.FMLControlledNamespacedRegistry;
import cpw.mods.fml.common.registry.GameData;
import gregtech.api.enums.Textures;
import gregtech.api.interfaces.IHasIndexedTexture;

/**
 * The texture pass (lane 6, issue #49): emit a block-to-icon manifest for the GT casing / coil
 * block families, headlessly, on the dedicated server - <em>Option A</em> of the texture pipeline
 * (plan section 5.2). No PNG is ever read or written here; the manifest maps
 * {@code (block_registry_name, meta, side)} to an icon name ({@code "modid:iconsets/NAME"}) to the
 * asset path inside the mod jar, which the previewer fetches from the Nexus jar at preview time
 * (LGPL assets stay out of this Apache-2.0 repo).
 *
 * <p>
 * <b>Why this works without a client.</b> In 1.7.10 {@code Block.getIcon(side, meta)} returns an
 * {@link IIcon} that is only populated by client-side texture registration, so on a dedicated server
 * it is normally {@code null} - and the register itself
 * ({@code net.minecraft.client.renderer.texture.IIconRegister}) is a {@code @SideOnly(CLIENT)} class
 * FML's {@code SideTransformer} refuses to even load on the server, so we cannot register icons the
 * client way. But GT wires every casing texture through a {@code Textures.BlockIcons} enum constant
 * whose name <em>is</em> the PNG filename: every constant's {@code run()} registers
 * {@code "gregtech:iconsets/" + name()}, and the PNG lives at
 * {@code assets/gregtech/textures/blocks/iconsets/<NAME>.png} (the constant's own
 * {@code getTextureFile()} is unusable here - it dereferences the client-only {@code TextureMap}
 * class the {@code SideTransformer} blocks). So we skip the register entirely: we reflectively set
 * every constant's package-private {@code mIcon} field (of the
 * server-safe {@code net.minecraft.util.IIcon} type) to a {@link NamedIcon} that carries the name,
 * then invoke each block's own {@code getIcon(side, meta)} <em>reflectively</em> (via
 * {@code MethodHandles.findVirtual}, so there is no compile-time reference to the possibly client-only
 * method). When the block's per-meta wiring references the icon constant directly, {@code getIcon}
 * hands back our {@link NamedIcon} and we read the name; no client, no icon register, and no
 * reimplementing the per-family switch. Where it instead goes through the {@code IIconContainer}
 * interface it cannot (see the documented gap below).
 *
 * <pre>
 *   reflectively set mIcon = NamedIcon("gregtech:iconsets/" + name()) on every BlockIcons constant
 *        |
 *        v
 *   for each registered block implementing IHasIndexedTexture (the casing/coil families):
 *        resolve its getIcon(int,int) override once (MethodHandles.findVirtual; skip if unresolvable)
 *        for each real meta, for sides 0..5 -> getIcon(side, meta) -> NamedIcon -> name + jar path
 *        collapse six identical sides to "all"; a block that names nothing becomes a recorded gap
 *        |
 *        v
 *   write <textureOut>/manifest.json (blocks -> icon names, icons -> jar paths, gaps, provenance)
 * </pre>
 *
 * <p>
 * <b>Scope: the GT casing / coil block families</b> (every registered block that is an
 * {@link IHasIndexedTexture}). These are the structural shell blocks - machine casings, heating
 * coils, tiered glass, pipe casings - that a multiblock is built from and that the previewer needs
 * to skin the shell. It deliberately does <em>not</em> depend on the structure dump's output (so the
 * texture workflow stays decoupled and can lag a pack version, per the plan), and it does not touch
 * {@link StructureDumper}.
 *
 * <p>
 * <b>What Option A can and cannot name (the documented gap).</b> A meta resolves when its
 * {@code getIcon} references the icon constant <em>directly</em> ({@code invokevirtual} on the
 * concrete {@code Textures$BlockIcons} enum, whose {@code getIcon()} is <em>not</em> side-stripped) -
 * e.g. the Electric Blast Furnace's heat-proof machine casing ({@code gt.blockcasings} meta 11 ->
 * {@code MACHINE_HEATPROOFCASING}). A meta does <em>not</em> resolve when {@code getIcon} selects its
 * sprite through the {@code IIconContainer} <em>interface</em> - a shared {@code MACHINECASINGS_*}
 * array or a switch stored as {@code IIconContainer} - because {@code IIconContainer.getIcon()} is
 * itself {@code @SideOnly(CLIENT)} and FML strips it from the interface on the server, so the
 * {@code invokeinterface} throws {@code NoSuchMethodError}. The heating coils
 * ({@code gt.blockcasings5}) hit exactly this path. Those metas, plus families with no server-side
 * {@code getIcon} override at all (GT's newer client-only {@code ITexture} render path) and the
 * composite tile-entity {@code gt.blockmachines} controller hulls, are recorded in {@code gaps} for
 * the documented <b>Option B</b> (client-mode {@code runClient} dump under {@code xvfb}) fallback -
 * never invented. This is the "Option A does not fully generalize" case the plan anticipates.
 */
final class TextureDumper {

    private static final Logger LOG = LogManager.getLogger(DumperMod.MODID);

    /** Manifest schema version. Bump when the on-disk shape changes. */
    static final int SCHEMA_VERSION = 1;

    /** Minecraft face indices passed to {@code getIcon(side, meta)}: down, up, N, S, W, E. */
    private static final int SIDES = 6;

    /** Deobf then SRG name for {@code Block.getIcon(int side, int meta)}, tried in order. */
    private static final String[] GET_ICON_NAMES = { "getIcon", "func_149691_a" };

    /** GT registers every casing sprite under this resource domain: {@code gregtech:iconsets/NAME}. */
    private static final String ICON_DOMAIN = "gregtech";

    private final Gson gson = new GsonBuilder().setPrettyPrinting()
        .disableHtmlEscaping()
        .create();

    /** A resolved icon: its registered name and the derived asset path inside the mod jar. */
    private static final class Icon {

        final String name;
        final String path;

        Icon(String name, String path) {
            this.name = name;
            this.path = path;
        }
    }

    /** One (block, meta, side) the pass could not resolve to a registered icon name. */
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

    /**
     * Reflect the icon names for every casing-family block and write {@code <textureOut>/manifest.json}.
     * Returns the number of resolved {@code (block, meta, side)} icon assignments.
     */
    int run(File textureOut, String packVersion, Map<String, String> modVersions, String extractorSha)
        throws IOException {
        textureOut.mkdirs();

        // block registry name -> meta -> side-key ("all" or "0".."5") -> icon name
        Map<String, Map<Integer, Map<String, String>>> blocks = new TreeMap<>();
        Map<String, String> sourceClasses = new TreeMap<>();
        Map<String, Icon> icons = new TreeMap<>();
        List<Gap> gaps = new ArrayList<>();

        int blockCount = 0;
        int metaCount = 0;
        int resolved = 0;

        populateIconNames();

        FMLControlledNamespacedRegistry<Block> registry = GameData.getBlockRegistry();
        // The FML block registry is a raw Iterable in this Forge version, so it yields Object;
        // filter to the indexed-texture (casing/coil) blocks and cast.
        for (Object entry : registry) {
            if (!(entry instanceof IHasIndexedTexture) || !(entry instanceof Block)) {
                continue;
            }
            Block block = (Block) entry;
            String registryName = String.valueOf(registry.getNameForObject(block));
            MethodHandle getIcon = findGetIcon(block);
            if (getIcon == null) {
                gaps.add(
                    new Gap(
                        registryName,
                        -1,
                        "all",
                        "no server-side getIcon(int,int) override (uses GT's client-only ITexture "
                            + "render path); resolve with Option B"));
                continue;
            }
            lastIconError = null;
            Map<Integer, Map<String, String>> perMeta = new TreeMap<>();
            for (int meta : realMetas(block)) {
                Map<String, String> sideMap = resolveSides(block, getIcon, registryName, meta, icons, gaps);
                if (!sideMap.isEmpty()) {
                    perMeta.put(meta, sideMap);
                    metaCount++;
                    resolved += sideMap.size();
                }
            }
            if (perMeta.isEmpty()) {
                // A resolvable getIcon that named nothing: most often the casing selects its sprite via
                // the @SideOnly IIconContainer.getIcon() interface method FML strips on the server
                // (array / switch-indexed metas, e.g. the heating coils). Record it, never drop it
                // silently, and point at Option B.
                gaps.add(
                    new Gap(
                        registryName,
                        -1,
                        "all",
                        "getIcon named no iconset sprite for any meta ("
                            + (lastIconError != null ? lastIconError : "unknown")
                            + "); resolve with Option B"));
                continue;
            }
            blocks.put(registryName, perMeta);
            sourceClasses.put(
                registryName,
                block.getClass()
                    .getName());
            blockCount++;
        }

        // gt.blockmachines controllers carry composite tile-entity overlay textures, not a single
        // registered casing sprite, so Option A cannot name them: record the family as an explicit
        // gap pointing at Option B (a client-mode xvfb dump), so the manifest states its own limits.
        gaps.add(
            new Gap(
                "gregtech:gt.blockmachines",
                -1,
                "all",
                "controller hull uses a composite tile-entity overlay texture, not a single "
                    + "registered iconset sprite; resolve with Option B (client-mode xvfb dump)"));

        writeManifest(
            new File(textureOut, "manifest.json"),
            packVersion,
            modVersions,
            extractorSha,
            blocks,
            sourceClasses,
            icons,
            gaps,
            blockCount,
            metaCount,
            resolved);

        LOG.info(
            "gtnh-extractor: texture manifest wrote {} blocks, {} metas, {} icons, {} (block,meta,side) "
                + "assignments, {} gaps.",
            blockCount,
            metaCount,
            icons.size(),
            resolved,
            gaps.size());
        return resolved;
    }

    /**
     * Reflectively point every {@code Textures.BlockIcons} constant's {@code mIcon} at a
     * {@link NamedIcon} carrying that constant's iconset name and jar path (derived from its
     * {@code name()}). This replaces the client-only {@code IIconRegister} sweep: the blocks'
     * {@code getIcon} bodies read {@code mIcon} back, so they now return a named icon on the
     * dedicated server. The mutation is harmless - the dump exits the JVM immediately after.
     */
    private void populateIconNames() {
        Field mIconField;
        try {
            mIconField = Textures.BlockIcons.class.getDeclaredField("mIcon");
            mIconField.setAccessible(true);
        } catch (NoSuchFieldException e) {
            throw new IllegalStateException("Textures.BlockIcons.mIcon field is gone: " + e.getMessage(), e);
        }
        int ok = 0;
        Textures.BlockIcons[] values = Textures.BlockIcons.values();
        for (Textures.BlockIcons icon : values) {
            try {
                Icon named = iconFor(icon);
                mIconField.set(icon, new NamedIcon(named.name, named.path));
                ok++;
            } catch (Throwable t) {
                LOG.debug("gtnh-extractor: cannot name BlockIcons.{}: {}", icon.name(), t.toString());
            }
        }
        LOG.info("gtnh-extractor: named {} of {} BlockIcons constants", ok, values.length);
    }

    /**
     * Resolve sides 0..5 for one (block, meta). Returns a side map: a single {@code "all"} entry when
     * every face carries the same icon (the common case for a casing), else one entry per resolved
     * face. Faces that stay {@code null} while others resolve are recorded as gaps.
     */
    private Map<String, String> resolveSides(Block block, MethodHandle getIcon, String registryName, int meta,
        Map<String, Icon> icons, List<Gap> gaps) {
        String[] names = new String[SIDES];
        int resolvedFaces = 0;
        for (int side = 0; side < SIDES; side++) {
            NamedIcon icon = iconAt(block, getIcon, side, meta);
            if (icon != null) {
                names[side] = icon.iconName;
                resolvedFaces++;
                icons.putIfAbsent(icon.iconName, new Icon(icon.iconName, icon.assetPath));
            }
        }
        Map<String, String> sideMap = new TreeMap<>();
        if (resolvedFaces == 0) {
            return sideMap; // not a resolvable sub-block; skip silently (may not be a real meta)
        }
        boolean uniform = true;
        for (int side = 1; side < SIDES; side++) {
            if (!equalName(names[0], names[side])) {
                uniform = false;
                break;
            }
        }
        if (uniform) {
            sideMap.put("all", names[0]);
            return sideMap;
        }
        for (int side = 0; side < SIDES; side++) {
            if (names[side] != null) {
                sideMap.put(Integer.toString(side), names[side]);
            } else {
                gaps.add(new Gap(registryName, meta, Integer.toString(side), "getIcon returned no named icon"));
            }
        }
        return sideMap;
    }

    /**
     * Invoke the block's own {@code getIcon(side, meta)} and read the injected {@link NamedIcon}, or
     * null. A null result records the reason in {@link #lastIconError} so the block-level gap can
     * explain it - most commonly a {@code NoSuchMethodError} because the casing selects its sprite
     * through the {@code @SideOnly(CLIENT)} {@code IIconContainer.getIcon()} interface method that FML
     * strips on the dedicated server (array / switch-indexed metas), which no server-side pass can
     * reach.
     */
    private NamedIcon iconAt(Block block, MethodHandle getIcon, int side, int meta) {
        try {
            Object icon = getIcon.invoke(block, side, meta);
            if (icon instanceof NamedIcon) {
                return (NamedIcon) icon;
            }
            lastIconError = icon == null ? "getIcon returned null (no injected icon)"
                : "getIcon returned a foreign icon";
        } catch (Throwable t) {
            lastIconError = t.getClass()
                .getSimpleName() + ": "
                + String.valueOf(t.getMessage());
            LOG.debug("gtnh-extractor: getIcon({}, {}) failed on {}: {}", side, meta, block.getClass(), lastIconError);
        }
        return null;
    }

    /** Why the most recent {@code getIcon} call yielded no named icon, for the block-level gap reason. */
    private String lastIconError;

    /**
     * Resolve the block's own {@code getIcon(int,int)} override as a {@link MethodHandle}. This is
     * deliberate: the base {@code Block.getIcon(int,int)} is {@code @SideOnly(CLIENT)} and stripped on
     * the dedicated server (a direct call throws {@code NoSuchMethodError}), while reflective
     * {@code getMethod}/{@code getDeclaredMethod} eagerly resolve a whole class method table and die
     * on the client-only {@code registerBlockIcons(IIconRegister)} the casings declare/inherit.
     * {@code findVirtual} does a <em>targeted</em> name+type resolution (like linking one
     * {@code invokevirtual}) that never touches that sibling, so it uniformly finds the casing's
     * un-stripped {@code getIcon} override on whichever class declares it.
     */
    private MethodHandle findGetIcon(Block block) {
        MethodType type = MethodType.methodType(IIcon.class, int.class, int.class);
        MethodHandles.Lookup lookup = MethodHandles.publicLookup();
        for (String name : GET_ICON_NAMES) {
            try {
                return lookup.findVirtual(block.getClass(), name, type);
            } catch (NoSuchMethodException | IllegalAccessException e) {
                // not this name (SRG vs deobf); try the next
            } catch (Throwable t) {
                LOG.debug("gtnh-extractor: cannot resolve getIcon on {}: {}", block.getClass(), t.toString());
            }
        }
        return null;
    }

    /**
     * The block's real sub-block metas, mirroring GT's own creative-list test (an
     * {@code ItemStack.getDisplayName()} that still contains {@code ".name"} is an unnamed, non-real
     * meta). Falls back to the full 0..15 casing range if server-side names are unavailable, so
     * nothing is missed.
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
                    // an item that dislikes a meta is simply not that sub-block
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

    private static boolean equalName(String a, String b) {
        return a == null ? b == null : a.equals(b);
    }

    /**
     * The iconset name and jar asset path for one {@code BlockIcons} constant. Every constant's
     * {@code run()} registers {@code Mods.GregTech.getResourcePath("iconsets", name())}, i.e.
     * {@code "gregtech:iconsets/<NAME>"}, and the PNG lives at
     * {@code assets/gregtech/textures/blocks/iconsets/<NAME>.png} (verified in the pinned jar). We
     * derive both from {@code name()} directly - the constant's own {@code getTextureFile()} is
     * unusable here because it dereferences the client-only {@code TextureMap} class, which FML's
     * {@code SideTransformer} refuses to load on the dedicated server.
     */
    private static Icon iconFor(Textures.BlockIcons icon) {
        String shortName = "iconsets/" + icon.name();
        String name = ICON_DOMAIN + ":" + shortName;
        String assetPath = "assets/" + ICON_DOMAIN + "/textures/blocks/" + shortName + ".png";
        return new Icon(name, assetPath);
    }

    private void writeManifest(File file, String packVersion, Map<String, String> modVersions, String extractorSha,
        Map<String, Map<Integer, Map<String, String>>> blocks, Map<String, String> sourceClasses,
        Map<String, Icon> icons, List<Gap> gaps, int blockCount, int metaCount, int resolved) throws IOException {
        JsonObject root = new JsonObject();
        root.addProperty("schema", SCHEMA_VERSION);
        root.addProperty("method", "server-icon-reflection");

        JsonObject provenance = new JsonObject();
        provenance.addProperty("pack_version", packVersion);
        JsonObject mods = new JsonObject();
        modVersions.forEach(mods::addProperty);
        provenance.add("mod_versions", mods);
        provenance.addProperty(
            "generated_at",
            java.time.Instant.now()
                .toString());
        provenance.addProperty("extractor_sha", extractorSha);
        provenance.addProperty(
            "note",
            "Icon names reflected server-side from GT Textures.BlockIcons (Option A). PNGs are NOT "
                + "committed (LGPL); fetch them from the GT5-Unofficial jar on the GTNH Nexus at "
                + "build/preview time using the paths in `icons`.");
        JsonObject coverage = new JsonObject();
        coverage.addProperty("blocks", blockCount);
        coverage.addProperty("metas", metaCount);
        coverage.addProperty("icons", icons.size());
        coverage.addProperty("assignments", resolved);
        coverage.addProperty("gaps", gaps.size());
        provenance.add("coverage", coverage);
        root.add("provenance", provenance);

        root.addProperty("asset_root", "assets/{modid}/textures/blocks/");

        JsonObject blocksJson = new JsonObject();
        blocks.forEach((registryName, perMeta) -> {
            JsonObject blockJson = new JsonObject();
            blockJson.addProperty("source_class", sourceClasses.get(registryName));
            JsonObject metasJson = new JsonObject();
            perMeta.forEach((meta, sideMap) -> {
                JsonObject sideJson = new JsonObject();
                sideMap.forEach(sideJson::addProperty);
                metasJson.add(Integer.toString(meta), sideJson);
            });
            blockJson.add("metas", metasJson);
            blocksJson.add(registryName, blockJson);
        });
        root.add("blocks", blocksJson);

        JsonObject iconsJson = new JsonObject();
        icons.forEach((name, icon) -> iconsJson.addProperty(name, icon.path));
        root.add("icons", iconsJson);

        JsonArray gapsJson = new JsonArray();
        gaps.stream()
            .sorted(
                java.util.Comparator.comparing((Gap g) -> g.block)
                    .thenComparingInt(g -> g.meta)
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

        file.getParentFile()
            .mkdirs();
        try (Writer w = Files.newBufferedWriter(file.toPath(), StandardCharsets.UTF_8)) {
            gson.toJson(root, w);
            w.write('\n');
        }
    }

    /**
     * An {@link IIcon} that carries only its registered name and jar asset path. {@code IIcon} is a
     * server-safe {@code net.minecraft.util} type (unlike the client-only {@code IIconRegister}), so
     * it can be injected into a {@code BlockIcons.mIcon} field on a dedicated server. Its UV/size
     * accessors return harmless defaults - nothing renders here; the name is the whole point.
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
