package net.gtnhsolver.extractor;

/**
 * The server half of the sided pair, and the reason the pair exists at all.
 *
 * <p>
 * Everything the auto-world harness touches ({@code Minecraft}, {@code GuiMainMenu},
 * {@code WorldSettings}) is client-only, and a class that so much as names those types cannot load
 * on a dedicated server. Keeping them behind a {@code @SidedProxy} means the server never loads
 * {@link ClientProxy} at all, rather than relying on a branch happening not to be taken.
 *
 * <p>
 * A dedicated server needs no harness: {@code runServer} loads its world by itself, which is why
 * the structure and texture passes have always run unattended there. This half is deliberately
 * empty.
 */
public class CommonProxy {

    /** Called from {@code FMLPreInitializationEvent}. Nothing to arrange on a server. */
    public void setUpAutoWorld() {}
}
