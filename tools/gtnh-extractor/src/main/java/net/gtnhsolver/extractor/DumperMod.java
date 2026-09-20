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
import cpw.mods.fml.common.SidedProxy;
import cpw.mods.fml.common.event.FMLPreInitializationEvent;
import cpw.mods.fml.common.event.FMLServerStartedEvent;

/**
 * Entrypoint for the GTNH physical-dataset extractor.
 *
 * <p>
 * The mod loads alongside GT5-Unofficial + StructureLib, waits for the server to finish starting,
 * runs the requested pass(es), and then terminates the JVM so that {@code ./gradlew runServer}
 * returns a shell exit code a caller can gate on: 0 on success, nonzero on any fatal failure.
 *
 * <p>
 * <b>Server or client.</b> {@link FMLServerStartedEvent} fires for a client's INTEGRATED server too
 * - the one that starts when a single-player world loads - and {@code MinecraftServer.getServer()}
 * is set for it just the same, so {@code ./gradlew runClient} runs the same passes after a world is
 * loaded. That is worth having because the texture pass resolves names differently there: in a
 * client JVM nothing is {@code @SideOnly}-stripped, so a sprite can simply be asked its name
 * ({@link TextureDumper}). The structure pass has no such client story and is not the reason to run
 * one: it swaps StructureLib's proxy to capture hints headlessly ({@link RecordingProxy}), which a
 * real client proxy would be drawing from. A texture-only run ({@code -PtextureOut} with no
 * {@code -PdatasetOut}) returns before that, which is the combination a client run should use.
 *
 * <p>
 * Two passes, gated independently by system properties (see {@link #dump()}): the structure dump
 * ({@link StructureDumper} + {@link JsonWriter} + {@link ErrorCollector}, iterating
 * {@code GregTechAPI.METATILEENTITIES} and calling each controller's {@code construct(...)}) under
 * {@code -PdatasetOut}, and the layered texture manifest ({@link TextureDumper}) under
 * {@code -PtextureOut}. A texture-only run skips the structure dump entirely.
 *
 * <p>
 * The GT5U / StructureLib API surface the dump loops touch is deliberately small and catalogued in
 * this tool's {@code README.md}; the design lives in {@code docs/dataset-extraction/}
 * (requirements.md, implementation.md, plan.md).
 */
@Mod(modid = DumperMod.MODID, version = Tags.VERSION, name = DumperMod.NAME, acceptedMinecraftVersions = "[1.7.10]")
public class DumperMod {

    public static final String MODID = "gtnhextractor";
    public static final String NAME = "GTNH Extractor";

    private static final Logger LOG = LogManager.getLogger(MODID);

    /**
     * The sided half of this mod, which exists solely for the client auto-world harness.
     *
     * <p>
     * {@link ClientProxy} names {@code Minecraft} and {@code GuiMainMenu}, types that do not exist
     * on a dedicated server, so it must never be loaded there. A {@code @SidedProxy} guarantees
     * that by construction; guarding a reference behind an {@code isClient()} branch would only
     * make it true in practice.
     */
    @SidedProxy(
        clientSide = "net.gtnhsolver.extractor.ClientProxy",
        serverSide = "net.gtnhsolver.extractor.CommonProxy")
    public static CommonProxy proxy;

    /**
     * Arrange the client auto-world harness, if {@code -PautoWorld=true} asked for one. A no-op on a
     * server, and a no-op on a client that did not ask.
     */
    @Mod.EventHandler
    public void preInit(FMLPreInitializationEvent event) {
        proxy.setUpAutoWorld();
    }

    /**
     * Fires once the server has fully started (world loaded, every mod's post-init complete) - the
     * point at which the GregTech registry is populated and a structure dump could run. On a client
     * that is the integrated server, so this fires when a single-player world finishes loading.
     * Runs the dump, then exits the JVM.
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
        exitJava(exitCode);
    }

    /** How long a graceful shutdown gets before {@link #exitJava} stops waiting for it. */
    private static final long EXIT_WATCHDOG_MS = 60_000L;

    /**
     * Ask FML to exit, and make sure that actually happens.
     *
     * <p>
     * The graceful path is still the one taken: {@code exitJava(code, false)} runs
     * {@code System.exit}, which lets FML and Forge shut the world down. But this is called from the
     * server thread, and a client JVM's shutdown hooks join that same thread - a shape that can sit
     * there forever instead of exiting. A hung shutdown would cost a whole run, and a client run
     * costs a human clicking through to a world, so a daemon watchdog halts the JVM if the graceful
     * path has not finished in time.
     *
     * <p>
     * Halting is safe by construction here and nowhere else: every output file is written, flushed
     * and closed before this method is reached, so the only thing lost is the throwaway world's
     * unsaved chunks. Whether the watchdog fired is in the log, which is how "did it exit cleanly"
     * gets an honest answer rather than an assumed one.
     */
    private static void exitJava(int exitCode) {
        Thread watchdog = new Thread(() -> {
            try {
                Thread.sleep(EXIT_WATCHDOG_MS);
            } catch (InterruptedException interrupted) {
                Thread.currentThread()
                    .interrupt();
                return;
            }
            LOG.warn("gtnh-extractor: shutdown stalled after {} ms; halting (output is written).", EXIT_WATCHDOG_MS);
            Runtime.getRuntime().halt(exitCode);
        }, "gtnh-extractor-exit-watchdog");
        watchdog.setDaemon(true);
        watchdog.start();
        FMLCommonHandler.instance()
            .exitJava(exitCode, false);
    }

    /**
     * Run the requested passes. Resolve the output directories and run metadata from system
     * properties (a local {@code ./gradlew runServer|runClient -PdatasetOut=... -PtextureOut=...}
     * passes them;
     * the structure dump is local-only with no CI, so the texture manifest is the only pass a
     * workflow drives). The structure pass builds every constructable controller with
     * {@link StructureDumper} and writes the schema-v2 dataset to {@code <datasetOut>/multiblocks/};
     * the texture pass writes the schema-2 manifest. Throws if a requested pass produced nothing, so
     * an extractor that silently emits an empty dataset fails loudly rather than being trusted.
     */
    private void dump() throws Exception {
        String packVersion = System.getProperty("gtnhextractor.packVersion", "unknown-dev");
        String extractorSha = resolveExtractorSha();
        Map<String, String> modVersions = collectModVersions();

        // Lane 6 v2 (issue #79): the layered texture manifest is a separate pass gated by
        // -PtextureOut. It reflects each MetaTileEntity's ITexture layer stack (which needs a booted
        // server + a scratch world to place hulls/hatches into) plus the plain casing block icons, so
        // when only -PtextureOut is set the correctness-critical structure dump is still skipped and
        // the texture workflow stays decoupled from it.
        File textureOut = resolveOut("gtnhextractor.textureOut");
        if (textureOut != null) {
            LOG.info("gtnh-extractor: dumping layered texture manifest to {}", textureOut.getAbsolutePath());
            World textureWorld = MinecraftServer.getServer().worldServers[0];
            int stacks = new TextureDumper(textureWorld).run(textureOut, packVersion, modVersions, extractorSha);
            if (stacks == 0) {
                throw new IllegalStateException("texture pass resolved no layer stacks; GT texture wiring changed");
            }
            LOG.info("gtnh-extractor: texture manifest complete ({} layer stacks).", stacks);
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
                versions.put(
                    pair.substring(0, eq)
                        .trim(),
                    pair.substring(eq + 1)
                        .trim());
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
