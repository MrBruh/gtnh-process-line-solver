package net.gtnhsolver.extractor;

import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.Predicate;

import net.minecraft.block.Block;
import net.minecraft.item.ItemStack;
import net.minecraft.tileentity.TileEntity;
import net.minecraft.world.World;

import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import com.gtnewhorizon.structurelib.structure.IStructureElement;
import com.gtnewhorizon.structurelib.structure.IStructureElementChain;

import gregtech.api.GregTechAPI;
import gregtech.api.enums.HatchElement;
import gregtech.api.interfaces.metatileentity.IMetaTileEntity;
import gregtech.api.metatileentity.BaseMetaTileEntity;
import gregtech.api.metatileentity.implementations.MTEBasicHull;

/**
 * Answers "what kind of hatch may sit in this cell?" for one structure element.
 *
 * <p>
 * The dump records a machine's geometry, but geometry alone cannot say which cells are I/O slots or
 * what they accept - and for some machines that is load-bearing. A Distillation Tower routes the
 * recipe's fluid output {@code i} to structure layer {@code i} and NOWHERE else, so the number of
 * layers that accept an output hatch is what decides how tall a tower a given recipe needs. Too
 * short a tower is still a legal multiblock; it just silently voids the fluids it has no layer for.
 *
 * <p>
 * Neither of the two obvious routes works. The hint pass only yields a dot index, which is a
 * machine-local integer the structure's author chose (and 13/14/15 are StructureLib's reserved
 * AIR/NOT_AIR/ERROR markers, not hatch data). Re-running the block pass with hatches enabled yields
 * nothing either: GT's hatch elements return an unconditional {@code false} from {@code placeBlock},
 * so {@code construct(...)} never places a hatch in the first place.
 *
 * <p>
 * So we ask the element itself, in one of two ways:
 *
 * <pre>
 *   each leaf of the visited element (a chain is walked into its branches)
 *        |
 *        v
 *   FILTER : getBlocksToPlace(...).getPredicate(), tested with one probe stack per HatchElement kind.
 *        |   On a freshly placed controller every hatch count is zero, so this is the element's
 *        |   MAXIMAL legal set. Most GT hatch elements (HatchElementBuilder) answer here.
 *        v   names no kind?
 *   CHECK  : put each probe hatch in the cell as a real GT tile and ask the element's own check(),
 *            which is the structure check a player's build passes or fails. A hatch adder built
 *            from a bare method reference (GTStructureUtility.ofHatchAdder) has no filter at all,
 *            and only answers here: the Distillation Tower's ring takes energy hatches this way.
 * </pre>
 *
 * <p>
 * The check probe is guarded by a control: a plain machine hull goes in first, and an element whose
 * check accepts that too takes any GT tile rather than a hatch, so it is recorded as taking none. It
 * runs once per element per controller ({@link #forgetCheckedElements}), since what a bare adder
 * accepts does not depend on the cell, and restores the cell's block afterwards. Every probe is
 * wrapped: a throwing element or predicate degrades to "no kinds", never to a failed dump.
 */
final class HatchProbe {

    private static final Logger LOG = LogManager.getLogger("gtnh-extractor");

    /** How deep a chain's branches are walked: StructureLib chains nest, but only a few levels. */
    private static final int MAX_CHAIN_DEPTH = 8;

    /** A hatch to probe with: its item form for the filter, and its MTE for placing a real one. */
    private static final class Probe {

        final ItemStack stack;
        final IMetaTileEntity mte;
        final int id;

        Probe(ItemStack stack, IMetaTileEntity mte, int id) {
            this.stack = stack;
            this.mte = mte;
            this.id = id;
        }
    }

    /** One probe per hatch kind, in {@link HatchElement} declaration order. */
    private final Map<String, Probe> probes = new LinkedHashMap<>();
    /** The check probe's control: a machine hull, which no hatch adder takes. Null if GT has none. */
    private final Probe hull;
    /** What each element's structure check took, per controller, by element identity. */
    private final Map<Object, Set<String>> checked = new IdentityHashMap<>();

    HatchProbe() {
        for (HatchElement kind : HatchElement.values()) {
            List<? extends Class<? extends IMetaTileEntity>> classes = kind.mteClasses();
            Probe probe = classes == null ? null : findProbe(classes);
            if (probe != null) {
                probes.put(kind.name(), probe);
            }
        }
        hull = findProbe(Collections.singletonList(MTEBasicHull.class));
        LOG.info(
            "gtnh-extractor: hatch probe built for {} of {} kinds (structure-check control: {})",
            probes.size(),
            HatchElement.values().length,
            hull != null ? "machine hull" : "none, so bare adders go unprobed");
    }

    /**
     * A representative hatch for one kind: the first registered MTE assignable to one of the classes the
     * kind declares. Declaration-driven, so a GT bump that renumbers hatches is picked up automatically
     * and only a kind GT stopped registering goes missing.
     */
    private static Probe findProbe(List<? extends Class<? extends IMetaTileEntity>> classes) {
        IMetaTileEntity[] all = GregTechAPI.METATILEENTITIES;
        for (int id = 0; id < all.length; id++) {
            IMetaTileEntity mte = all[id];
            if (mte == null) {
                continue;
            }
            for (Class<? extends IMetaTileEntity> cls : classes) {
                if (cls != null && cls.isInstance(mte)) {
                    ItemStack form = mte.getStackForm(1);
                    if (form != null) {
                        return new Probe(form, mte, id);
                    }
                }
            }
        }
        return null;
    }

    /** Drop what the structure checks answered: a new controller's elements are asked afresh. */
    void forgetCheckedElements() {
        checked.clear();
    }

    /**
     * The hatch kinds {@code element} accepts at {@code (x,y,z)}, sorted for a stable dump. Empty for
     * a cell that accepts no hatch (plain casing, air, or an element that takes any GT tile).
     */
    Set<String> kindsAt(IStructureElement<Object> element, Object controller, World world, int x, int y, int z,
        ItemStack trigger) {
        Set<String> kinds = new LinkedHashSet<>();
        for (IStructureElement<Object> leaf : flatten(element)) {
            Set<String> filtered = filterKinds(leaf, controller, world, x, y, z, trigger);
            kinds.addAll(filtered.isEmpty() ? checkedKinds(leaf, controller, world, x, y, z) : filtered);
        }
        List<String> ordered = new ArrayList<>(kinds);
        Collections.sort(ordered);
        return new LinkedHashSet<>(ordered);
    }

    /** The kinds {@code leaf}'s item filter admits, one probe stack per kind. */
    private Set<String> filterKinds(IStructureElement<Object> leaf, Object controller, World world, int x, int y,
        int z, ItemStack trigger) {
        Set<String> kinds = new LinkedHashSet<>();
        Predicate<ItemStack> accepts = predicateOf(leaf, controller, world, x, y, z, trigger);
        if (accepts == null) {
            return kinds;
        }
        for (Map.Entry<String, Probe> probe : probes.entrySet()) {
            try {
                if (accepts.test(probe.getValue().stack)) {
                    kinds.add(probe.getKey());
                }
            } catch (Exception | LinkageError e) {
                // A predicate that trips on a probe tells us nothing about this kind; keep going
                // rather than lose the kinds the other probes did answer.
                LOG.debug("gtnh-extractor: hatch predicate threw for {} at {},{},{}", probe.getKey(), x, y, z);
            }
        }
        return kinds;
    }

    /**
     * The kinds {@code leaf}'s own structure check accepts, asked by standing each probe hatch in the
     * cell (see the class comment). Memoised per element for the current controller. A cell holding a
     * tile entity is not disturbed: the element is asked at the next cell it governs instead.
     */
    private Set<String> checkedKinds(IStructureElement<Object> leaf, Object controller, World world, int x, int y,
        int z) {
        Set<String> known = checked.get(leaf);
        if (known != null) {
            return known;
        }
        if (hull == null) {
            return Collections.emptySet();
        }
        Block original = world.getBlock(x, y, z);
        int originalMeta = world.getBlockMetadata(x, y, z);
        if (original == null || original.hasTileEntity(originalMeta)) {
            return Collections.emptySet();
        }
        Set<String> kinds = new LinkedHashSet<>();
        try {
            if (!accepts(leaf, controller, world, x, y, z, hull)) {
                for (Map.Entry<String, Probe> probe : probes.entrySet()) {
                    if (accepts(leaf, controller, world, x, y, z, probe.getValue())) {
                        kinds.add(probe.getKey());
                    }
                }
            }
        } finally {
            world.setBlock(x, y, z, original, originalMeta, 2);
        }
        checked.put(leaf, kinds);
        return kinds;
    }

    /** Whether {@code leaf}'s structure check passes with {@code probe}'s hatch standing at the cell. */
    private static boolean accepts(IStructureElement<Object> leaf, Object controller, World world, int x, int y,
        int z, Probe probe) {
        try {
            return place(world, x, y, z, probe) && leaf.check(controller, world, x, y, z);
        } catch (Exception | LinkageError e) {
            return false; // an adder that throws on a stranger's hatch does not take it
        }
    }

    /**
     * Stand a real GT tile of {@code probe}'s machine at the cell, the way {@code StructureDumper} places
     * its controller: the machine block, then the MTE set on the base tile it creates.
     */
    private static boolean place(World world, int x, int y, int z, Probe probe) {
        Block block = Block.getBlockFromItem(probe.stack.getItem());
        if (block == null) {
            return false;
        }
        world.setBlock(x, y, z, block, 0, 2);
        TileEntity te = world.getTileEntity(x, y, z);
        if (!(te instanceof BaseMetaTileEntity)) {
            return false;
        }
        BaseMetaTileEntity base = (BaseMetaTileEntity) te;
        base.setMetaTileID((short) probe.id);
        IMetaTileEntity mte = probe.mte.newMetaEntity(base);
        if (mte == null) {
            return false;
        }
        base.setMetaTileEntity(mte);
        mte.setBaseMetaTileEntity(base);
        return true;
    }

    /** {@code element} and, when it is a chain, every branch below it, depth first. */
    private static List<IStructureElement<Object>> flatten(IStructureElement<Object> element) {
        List<IStructureElement<Object>> out = new ArrayList<>();
        flattenInto(element, out, 0);
        return out;
    }

    @SuppressWarnings("unchecked")
    private static void flattenInto(IStructureElement<Object> element, List<IStructureElement<Object>> out,
        int depth) {
        out.add(element);
        if (depth >= MAX_CHAIN_DEPTH || !(element instanceof IStructureElementChain)) {
            return;
        }
        IStructureElement<Object>[] fallbacks = ((IStructureElementChain<Object>) element).fallbacks();
        if (fallbacks == null) {
            return;
        }
        for (IStructureElement<Object> fallback : fallbacks) {
            if (fallback != null) {
                flattenInto(fallback, out, depth + 1);
            }
        }
    }

    /**
     * The accept-predicate for one element, or {@code null} if it exposes none.
     *
     * <p>
     * The {@code AutoPlaceEnvironment} argument is passed as {@code null}: GT's hatch element ignores
     * it (its filter is a function of the controller and trigger only), which is what makes this
     * probe possible outside a real autoplace. An element that does dereference it throws, and is
     * treated as "exposes no predicate" - the same as a plain casing.
     */
    private static Predicate<ItemStack> predicateOf(IStructureElement<Object> element, Object controller, World world,
        int x, int y, int z, ItemStack trigger) {
        try {
            IStructureElement.BlocksToPlace blocks = element
                .getBlocksToPlace(controller, world, x, y, z, trigger, null);
            return blocks == null ? null : blocks.getPredicate();
        } catch (Exception | LinkageError e) {
            return null;
        }
    }
}
