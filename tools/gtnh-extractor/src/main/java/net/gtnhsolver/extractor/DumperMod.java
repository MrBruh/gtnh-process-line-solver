package net.gtnhsolver.extractor;

import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import cpw.mods.fml.common.FMLCommonHandler;
import cpw.mods.fml.common.Mod;
import cpw.mods.fml.common.event.FMLServerStartedEvent;

/**
 * Headless entrypoint for the GTNH structure-dataset extractor.
 *
 * <p>This is the lane 1 scaffold (issue #44). The mod loads on a dedicated server
 * alongside GT5-Unofficial + StructureLib, waits for the server to finish starting, runs
 * the dump, and then terminates the JVM so that {@code ./gradlew runServer} returns a
 * shell exit code CI can gate on: 0 on success, nonzero on any fatal failure.
 *
 * <p>The dump body itself is intentionally empty here. The real extraction loop
 * (StructureDumper + JsonWriter + ErrorCollector, iterating
 * {@code GregTechAPI.METATILEENTITIES} and calling each controller's
 * {@code construct(...)}) lands in lane 2 (issue #45). Landing the boot/exit plumbing
 * first means lane 2 drops into a seam that a real server boot already exercises.
 *
 * <p>GT5U / StructureLib API surface touched by this class: none yet. It references only
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
     * <p>Exit code contract: 0 when the dump succeeds so CI goes green; nonzero when any
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
        FMLCommonHandler.instance().exitJava(exitCode, false);
    }

    /**
     * Lane 1: a no-op that only proves the scaffold boots. Lane 2 (issue #45) replaces
     * this with the StructureDumper loop over {@code GregTechAPI.METATILEENTITIES}.
     */
    private void dump() {
        LOG.info("gtnh-extractor: scaffold boot OK; dump loop not yet implemented (lane 2, issue #45).");
    }
}
