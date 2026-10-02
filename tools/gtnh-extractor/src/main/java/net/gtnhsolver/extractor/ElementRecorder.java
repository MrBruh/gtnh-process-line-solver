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
 *
 * <p>
 * <b>Only the build's own walk is recorded.</b> From GT 5.09.54.133 a hatch element's
 * {@code placeBlock} runs the controller's whole structure check ({@code checkStructure}) from inside
 * the build, even when it places nothing, and that check walks the structure through the same
 * {@code iterateV2}, still on this thread and token. Its visits are not the build's: it walks the
 * pieces {@code checkMachine} chooses, at the offsets it chooses, as far as its elements pass, so it
 * can reach a cell ahead of the build front (an element that takes air, or an alternative piece the
 * build never places) and name it with an element the build would not. First visit wins, so such a
 * visit would decide the cell, and a hatch slot could be read off a piece that is not there. A visit
 * made under a {@code checkStructure} frame is therefore ignored. Only a new cell's visit is
 * checked, since a cell already recorded is not changed either way, which keeps the stack walk off
 * the common path.
 */
public final class ElementRecorder {

    // Public, unlike its siblings in this package: Forge's EventBus registers a handler by generating
    // an ASM wrapper in its OWN package, which then references this class and its @SubscribeEvent
    // method directly. Package-private here means every dispatch dies with an IllegalAccessError.

    /** The GT method a nested structure check runs under (MTEMultiBlockBase and its overrides). */
    private static final String STRUCTURE_CHECK = "checkStructure";

    /** Identity token this recorder accepts events for (StructureLib echoes it back on each event). */
    private final Object token;
    /** Visited cells, keyed by packed world position, in visit order. */
    private final Map<Long, IStructureElement<?>> byCell = new LinkedHashMap<>();
    /** Visits of a new cell made by a structure check running inside the build, and so ignored. */
    private int nestedIgnored;

    ElementRecorder(Object token) {
        this.token = token;
    }

    @SubscribeEvent
    public void onElementVisited(StructureEvent.StructureElementVisitedEvent event) {
        if (event.getInstrumentIdentifier() != token) {
            return;
        }
        long cell = pack(event.getX(), event.getY(), event.getZ());
        // First visit wins: a chain re-visits a cell as it walks its branches, and the outermost
        // element is the one that describes what the cell may hold.
        if (byCell.containsKey(cell)) {
            return;
        }
        if (insideStructureCheck()) {
            nestedIgnored++;
            return;
        }
        byCell.put(cell, event.getElement());
    }

    /** Whether this event was raised by a structure check nested in the build (see the class comment). */
    private static boolean insideStructureCheck() {
        for (StackTraceElement frame : new Throwable().getStackTrace()) {
            if (STRUCTURE_CHECK.equals(frame.getMethodName())) {
                return true;
            }
        }
        return false;
    }

    /** How many new-cell visits came from a nested structure check and were ignored. */
    int nestedIgnored() {
        return nestedIgnored;
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
