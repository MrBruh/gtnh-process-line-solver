package net.gtnhsolver.extractor;

import java.io.File;
import java.util.LinkedHashMap;
import java.util.Map;

import net.minecraft.server.MinecraftServer;
import net.minecraft.world.World;

import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import cpw.mods.fml.common.FMLCommonHandler;
import cpw.mods.fml.common.Loader;
import cpw.mods.fml.common.Mod;
import cpw.mods.fml.common.ModContainer;
import cpw.mods.fml.common.event.FMLServerStartedEvent;

/**
 * Headless entrypoint for the GTNH structure-dataset extractor.
 *
 * <p>
 * This is the lane 1 scaffold (issue #44). The mod loads on a dedicated server
 * alongside GT5-Unofficial + StructureLib, waits for the server to finish starting, runs
 * the dump, and then terminates the JVM so that {@code ./gradlew runServer} returns a
 * shell exit code CI can gate on: 0 on success, nonzero on any fatal failure.
 *
 * <p>
 * The dump body itself is intentionally empty here. The real extraction loop
 * (StructureDumper + JsonWriter + ErrorCollector, iterating
 * {@code GregTechAPI.METATILEENTITIES} and calling each controller's
 * {@code construct(...)}) lands in lane 2 (issue #45). Landing the boot/exit plumbing
 * first means lane 2 drops into a seam that a real server boot already exercises.
 *
 * <p>
 * GT5U / StructureLib API surface touched by this class: none yet. It references only
 * Forge/FML ({@link FMLServerStartedEvent} and
 * {@link FMLCommonHandler#exitJava(int, boolean)}). The intended (deliberately tiny) GT5U
 * surface for the dump loop is catalogued in this tool's {@code README.md}.
 */
@Mod(modid = DumperMod.MODID, version = Tags.VERSION, name = DumperMod.NAME, acceptedMinecraftVersions = "[1.7.10]")
public class DumperMod {

    public static final String MODID = "gtnhextractor";
    public static final String NAME = "GTNH Extractor";

    private static final Logger LOG = LogManager.getLogger(MODID);

    /**
     * Fires once the dedicated server has fully started (world loaded, every mod's
     * post-init complete) - the point at which the GregTech registry is populated and a
     * structure dump could run. Runs the dump, then exits the JVM.
     *
     * <p>
     * Exit code contract: 0 when the dump succeeds so CI goes green; nonzero when any
     * {@link Throwable} escapes the dump so CI fails loudly rather than committing an
     * empty or partial dataset. {@code hardExit = false} lets FML shut the server down
     * gracefully before the process exits.
     */
    @Mod.EventHandler
    public void onServerStarted(FMLServerStartedEvent event) {
        int exitCode;
        try {
            dump();
            LOG.info("gtnh-extractor: dump complete, shutting the server down cleanly.");
            exitCode = 0;
        } catch (Throwable t) {
            LOG.error("gtnh-extractor: dump failed, aborting with a nonzero exit code.", t);
            exitCode = 1;
        }
        FMLCommonHandler.instance()
            .exitJava(exitCode, false);
    }

    /**
     * Lane 2 (issue #45): run the core dump. Resolve the output directory and run metadata from
     * system properties (the lane-4 workflow passes them via {@code -PdatasetOut=...} etc.), build
     * every constructable controller with {@link StructureDumper}, and write the schema-v1 dataset
     * to {@code <datasetOut>/multiblocks/}. Throws if nothing was dumped, so an extractor that
     * silently produces an empty dataset fails CI rather than committing it.
     */
    private void dump() throws Exception {
        String packVersion = System.getProperty("gtnhextractor.packVersion", "unknown-dev");
        String extractorSha = resolveExtractorSha();
        Map<String, String> modVersions = collectModVersions();

        // Lane 6 (issue #49): the texture manifest is a separate pass gated by -PtextureOut. It runs
        // headlessly with no structure build (it only reflects GT's icon wiring off the block
        // registry), so when only -PtextureOut is set the correctness-critical structure dump is
        // skipped and the texture workflow boots fast and stays decoupled from it.
        File textureOut = resolveOut("gtnhextractor.textureOut");
        if (textureOut != null) {
            LOG.info("gtnh-extractor: dumping texture manifest to {}", textureOut.getAbsolutePath());
            int icons = new TextureDumper().run(textureOut, packVersion, modVersions, extractorSha);
            if (icons == 0) {
                throw new IllegalStateException("texture pass resolved no icons; the casing families changed");
            }
            LOG.info("gtnh-extractor: texture manifest complete ({} icon assignments).", icons);
        }
        if (textureOut != null && resolveOut("gtnhextractor.datasetOut") == null) {
            return; // texture-only run: skip the structure dump entirely
        }

        File datasetOut = resolveDatasetOut();
        World world = MinecraftServer.getServer().worldServers[0];
        LOG.info(
            "gtnh-extractor: dumping multiblocks to {} (pack {}, extractor {})",
            datasetOut.getAbsolutePath(),
            packVersion,
            extractorSha);

        StructureDumper dumper = new StructureDumper(world);
        int written = dumper.run(datasetOut, packVersion, modVersions, extractorSha);
        if (written == 0) {
            throw new IllegalStateException("dump produced no controllers; see the failure list in _meta.json");
        }
        LOG.info(
            "gtnh-extractor: wrote {} controllers, {} failures.",
            written,
            dumper.errors()
                .count());
    }

    /** Resolve an output-directory system property to a {@link File}, or {@code null} if unset/blank. */
    private File resolveOut(String propKey) {
        String configured = System.getProperty(propKey);
        if (configured != null && !configured.trim()
            .isEmpty()) {
            return new File(configured);
        }
        return null;
    }

    /** {@code -PdatasetOut} is forwarded as a system property by the build; default to an in-tree dir. */
    private File resolveDatasetOut() {
        String configured = System.getProperty("gtnhextractor.datasetOut");
        if (configured != null && !configured.trim()
            .isEmpty()) {
            return new File(configured);
        }
        return new File(System.getProperty("user.dir"), "dataset-out");
    }

    /** Prefer an explicit property, then the CI-provided commit SHA, else a non-empty placeholder. */
    private String resolveExtractorSha() {
        String sha = System.getProperty("gtnhextractor.extractorSha");
        if (sha == null || sha.trim()
            .isEmpty()) {
            sha = System.getenv("GITHUB_SHA");
        }
        return sha != null && !sha.trim()
            .isEmpty() ? sha : "unknown";
    }

    /**
     * The versions of the two manifest-tracked mods this dump was built from, for {@code _meta.json}.
     * Prefers the pinned versions passed by the workflow via {@code -PmodVersions} (read from the
     * repo-root {@code gtnh.lock.json}); the runtime Forge container is only the dev fallback,
     * because GT5-Unofficial's container self-reports the uninformative "MC1710" rather than its
     * artifact version.
     */
    private Map<String, String> collectModVersions() {
        Map<String, String> versions = new LinkedHashMap<>();
        String pinned = System.getProperty("gtnhextractor.modVersions", "");
        for (String pair : pinned.split(",")) {
            int eq = pair.indexOf('=');
            if (eq > 0) {
                versions.put(pair.substring(0, eq).trim(), pair.substring(eq + 1).trim());
            }
        }
        putModVersion(versions, "GT5-Unofficial", "gregtech");
        putModVersion(versions, "StructureLib", "structurelib");
        return versions;
    }

    private void putModVersion(Map<String, String> versions, String label, String modId) {
        ModContainer container = Loader.instance()
            .getIndexedModList()
            .get(modId);
        if (container != null) {
            versions.putIfAbsent(label, container.getVersion());
        }
    }
}
