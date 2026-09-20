package net.gtnhsolver.extractor;

import java.util.Random;

import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.GuiMainMenu;
import net.minecraft.world.WorldSettings;
import net.minecraft.world.WorldType;

import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import cpw.mods.fml.common.FMLCommonHandler;
import cpw.mods.fml.common.eventhandler.SubscribeEvent;
import cpw.mods.fml.common.gameevent.TickEvent;

/**
 * Loads a scratch world by itself, so a client dump needs no human at the keyboard.
 *
 * <p>
 * The texture pass resolves names far better in a client JVM (see
 * {@code docs/dataset-extraction/client-dump-spike.md}), but {@link DumperMod} only fires on
 * {@code FMLServerStartedEvent}, and on a client that means someone has to click through
 * Singleplayer, Create New World, Create New World. That was five manual runs during the spike, and
 * it is the one thing keeping the client route from being scriptable.
 *
 * <p>
 * <b>There is no hidden machinery here.</b> "Create New World" does exactly one thing
 * ({@code GuiCreateWorld}, MCP 1.7.10): it builds a {@link WorldSettings} and calls
 * {@code Minecraft.launchIntegratedServer(folder, name, settings)}. This waits for the main menu to
 * appear and makes that same call.
 *
 * <p>
 * <b>Opt-in, via {@code -PautoWorld=true}.</b> Off by default, because {@code runClient} is also how
 * a person plays the dev environment and a client that swallows the main menu would be hostile.
 *
 * <p>
 * <b>It deletes only the folder it creates.</b> {@link #SCRATCH_FOLDER} is a name no human would
 * pick, and the delete is by that exact name. The harness never enumerates saves and never removes a
 * world it did not make, which matters because this runs in a workspace that also holds worlds
 * somebody clicked into being.
 */
public class ClientProxy extends CommonProxy {

    private static final Logger LOG = LogManager.getLogger(DumperMod.MODID);

    /** The save folder this harness owns, creates, and is allowed to delete. */
    private static final String SCRATCH_FOLDER = "gtnhextractor-scratch";

    /** Ticks to wait at the main menu before acting, so the menu is settled rather than mid-init. */
    private static final int SETTLE_TICKS = 20;

    @Override
    public void setUpAutoWorld() {
        if (!Boolean.parseBoolean(System.getProperty("gtnhextractor.autoWorld", "false"))) {
            return;
        }
        LOG.info("gtnh-extractor: auto-world armed; will load '{}' once the main menu appears", SCRATCH_FOLDER);
        FMLCommonHandler.instance()
            .bus()
            .register(this);
    }

    private int menuTicks;
    private boolean launched;

    /**
     * Wait for the main menu, then create the scratch world.
     *
     * <p>
     * Keyed off {@code currentScreen} rather than a fixed delay, because a GTNH client's startup
     * time is not predictable. If the main menu never appears (a mod showing its own first-run
     * screen, say) this simply never fires and the run stays clickable by hand, which is the right
     * way for a convenience to fail.
     */
    @SubscribeEvent
    public void onClientTick(TickEvent.ClientTickEvent event) {
        if (launched || event.phase != TickEvent.Phase.END) {
            return;
        }
        Minecraft mc = Minecraft.getMinecraft();
        if (!(mc.currentScreen instanceof GuiMainMenu)) {
            menuTicks = 0;
            return;
        }
        if (++menuTicks < SETTLE_TICKS) {
            return;
        }
        launched = true;
        try {
            launchScratchWorld(mc);
        } catch (Throwable t) {
            // Never take the client down over a convenience. A failure here leaves the main menu
            // sitting there, which is exactly the state a human can rescue by clicking.
            LOG.error("gtnh-extractor: auto-world failed; load a world by hand to run the dump", t);
        }
    }

    private void launchScratchWorld(Minecraft mc) {
        if (mc.getSaveLoader()
            .canLoadWorld(SCRATCH_FOLDER)) {
            boolean deleted = mc.getSaveLoader()
                .deleteWorldDirectory(SCRATCH_FOLDER);
            LOG.info("gtnh-extractor: removed the previous scratch world ({})", deleted ? "ok" : "FAILED");
        }
        // Superflat, no structures, creative: the dump reads block definitions and places a few
        // hulls at a scratch origin, so terrain is pure cost. This is what takes "Preparing spawn
        // area" from tens of seconds to almost none.
        WorldSettings settings = new WorldSettings(
            new Random().nextLong(),
            WorldSettings.GameType.CREATIVE,
            false,
            false,
            WorldType.FLAT);
        LOG.info("gtnh-extractor: auto-world creating '{}' (superflat, creative)", SCRATCH_FOLDER);
        mc.launchIntegratedServer(SCRATCH_FOLDER, SCRATCH_FOLDER, settings);
    }
}
