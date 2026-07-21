package net.gtnhsolver.extractor;

import java.util.LinkedHashMap;
import java.util.Map;

import com.gtnewhorizon.structurelib.StructureEvent;
import com.gtnewhorizon.structurelib.structure.IStructureElement;

import cpw.mods.fml.common.eventhandler.SubscribeEvent;

/**
 * Records which {@link IStructureElement} StructureLib visited at each world cell during one build.
 *
 * <p>
 * StructureLib funnels its check, hint and place walks through a single {@code iterateV2}, which
 * fires a {@link StructureEvent.StructureElementVisitedEvent} per visited cell whenever
 * instrumentation is enabled. Nothing in StructureLib or GT consumes that event - it exists for
 * third-party tooling, which is exactly what this extractor is. It is the only route that yields
 * cell -> element, and it attaches to the BLOCK pass, which already runs server-side with no
 * client-only rendering on the path (unlike the hint pass, which has to flip {@code world.isRemote}
 * and is best-effort as a result).
 *
 * <p>
 * Instrumentation is process-wide and keyed by an identity token, so every event is filtered back to
 * the token this recorder was opened with; a stray event from anything else is ignored rather than
 * silently merged into the dump.
 */
public final class ElementRecorder {

    // Public, unlike its siblings in this package: Forge's EventBus registers a handler by generating
    // an ASM wrapper in its OWN package, which then references this class and its @SubscribeEvent
    // method directly. Package-private here means every dispatch dies with an IllegalAccessError.

    /** Identity token this recorder accepts events for (StructureLib echoes it back on each event). */
    private final Object token;
    /** Visited cells, keyed by packed world position, in visit order. */
    private final Map<Long, IStructureElement<?>> byCell = new LinkedHashMap<>();

    ElementRecorder(Object token) {
        this.token = token;
    }

    @SubscribeEvent
    public void onElementVisited(StructureEvent.StructureElementVisitedEvent event) {
        if (event.getInstrumentIdentifier() != token) {
            return;
        }
        // First visit wins: a chain re-visits a cell as it walks its branches, and the outermost
        // element is the one that describes what the cell may hold.
        byCell.putIfAbsent(pack(event.getX(), event.getY(), event.getZ()), event.getElement());
    }

    /** The element recorded at a world cell, or {@code null} if the walk never visited it. */
    IStructureElement<?> at(int x, int y, int z) {
        return byCell.get(pack(x, y, z));
    }

    int size() {
        return byCell.size();
    }

    /**
     * Pack a world position into one long. The scratch region sits near the origin and well inside
     * +/-2^20 on every axis, so 21 bits per axis is comfortable and collision-free here.
     */
    private static long pack(int x, int y, int z) {
        return ((long) (x & 0x1FFFFF) << 42) | ((long) (y & 0x1FFFFF) << 21) | (z & 0x1FFFFF);
    }
}
