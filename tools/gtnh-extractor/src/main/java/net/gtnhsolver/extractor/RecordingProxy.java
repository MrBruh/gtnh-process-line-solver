package net.gtnhsolver.extractor;

import net.minecraft.block.Block;
import net.minecraft.util.IIcon;
import net.minecraft.world.World;

import com.gtnewhorizon.structurelib.CommonProxy;

/**
 * A {@link CommonProxy} that captures every hint particle a {@code construct(trigger, true)}
 * (hints-only) build emits, so hint dots can be read <em>headlessly</em>.
 *
 * <p>
 * Why this exists: StructureLib routes {@code StructureLibAPI.hintParticle*} through the
 * active {@code StructureLib.proxy}. On a dedicated server that proxy is a plain {@link CommonProxy}
 * whose hint methods are no-ops (particles are a client-render concern). The in-game hologram
 * projector only shows dots because the client proxy draws them. To get the same information
 * without a client, {@link StructureDumper} temporarily swaps this recorder in for the duration of
 * a hint pass: each element's {@code spawnHint} then reports its cell and block here instead of
 * into the void.
 *
 * <p>
 * Two facts are recorded per call: the cell coordinate (every element in the structure reports
 * one, giving the full occupied region) and, when the hinted block is StructureLib's hint block, a
 * hint dot with its meta (the dot colour StructureLib chose - opaque to us, kept for fidelity).
 * Casing/coil elements hint their real block, which is how a hatch slot is told apart from a solid
 * cell. Everything else this proxy inherits unchanged from {@link CommonProxy}.
 */
final class RecordingProxy extends CommonProxy {

    /** Callback invoked for each hinted cell. {@code block} may be {@code null} for icon-only hints. */
    interface Sink {

        void particle(World world, int x, int y, int z, Block block, int meta);
    }

    private final Sink sink;

    RecordingProxy(Sink sink) {
        this.sink = sink;
    }

    @Override
    public void hintParticleTinted(World world, int x, int y, int z, Block block, int meta, short[] rgba) {
        sink.particle(world, x, y, z, block, meta);
    }

    @Override
    public void hintParticleTinted(World world, int x, int y, int z, IIcon[] icons, short[] rgba) {
        sink.particle(world, x, y, z, null, 0);
    }

    @Override
    public void hintParticle(World world, int x, int y, int z, Block block, int meta) {
        sink.particle(world, x, y, z, block, meta);
    }

    @Override
    public void hintParticle(World world, int x, int y, int z, IIcon[] icons) {
        sink.particle(world, x, y, z, null, 0);
    }
}
