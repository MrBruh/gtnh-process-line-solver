package net.gtnhsolver.extractor;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.Predicate;

import net.minecraft.item.ItemStack;
import net.minecraft.world.World;

import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import com.gtnewhorizon.structurelib.structure.IStructureElement;
import com.gtnewhorizon.structurelib.structure.IStructureElementChain;

import gregtech.api.GregTechAPI;
import gregtech.api.enums.HatchElement;
import gregtech.api.interfaces.metatileentity.IMetaTileEntity;

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
 * AIR/NOT_AIR/ERROR markers, not hatch data). Re-running the block pass without the
 * {@code gt_no_hatch} channel yields nothing either: GT's hatch elements return an unconditional
 * {@code false} from {@code placeBlock}, so {@code construct(...)} never places a hatch in the first
 * place and the channel only affects the player-driven survival autobuild path.
 *
 * <p>
 * So we ask the element itself. {@link IStructureElement#getBlocksToPlace} returns the set of stacks
 * that element would accept, as a {@link Predicate}; testing one probe stack per {@link HatchElement}
 * kind against it recovers the accepted kinds. On a freshly placed controller every hatch count is
 * zero, so the predicate is the element's MAXIMAL legal set - exactly the question being asked.
 *
 * <p>
 * Two deliberate limits. A hatch adder built from a bare method reference
 * ({@code GTStructureUtility.ofHatchAdder}) carries no item filter at all, so such a cell reports no
 * kinds rather than a wrong guess. And {@code count()} is controller-state dependent in general, so
 * every probe is wrapped per cell: a throwing element degrades to "no kinds", never to a failed dump.
 */
final class HatchProbe {

    private static final Logger LOG = LogManager.getLogger("gtnh-extractor");

    /** One probe stack per hatch kind, in {@link HatchElement} declaration order. */
    private final Map<String, ItemStack> probes = new LinkedHashMap<>();

    HatchProbe() {
        for (HatchElement kind : HatchElement.values()) {
            ItemStack probe = findProbe(kind);
            if (probe != null) {
                probes.put(kind.name(), probe);
            }
        }
        LOG.info("gtnh-extractor: hatch probe built for {} of {} kinds", probes.size(), HatchElement.values().length);
    }

    /**
     * A representative stack for {@code kind}: the first registered MTE assignable to one of the
     * classes the kind declares. Declaration-driven, so a GT bump that renumbers hatches is picked up
     * automatically and only a kind GT stopped registering goes missing.
     */
    private static ItemStack findProbe(HatchElement kind) {
        List<? extends Class<? extends IMetaTileEntity>> classes = kind.mteClasses();
        if (classes == null || classes.isEmpty()) {
            return null;
        }
        for (IMetaTileEntity mte : GregTechAPI.METATILEENTITIES) {
            if (mte == null) {
                continue;
            }
            for (Class<? extends IMetaTileEntity> cls : classes) {
                if (cls != null && cls.isInstance(mte)) {
                    ItemStack form = mte.getStackForm(1);
                    if (form != null) {
                        return form;
                    }
                }
            }
        }
        return null;
    }

    /**
     * The hatch kinds {@code element} accepts at {@code (x,y,z)}, sorted for a stable dump.
     *
     * <p>
     * A chain is walked into its fallbacks as well as probed directly: the chain's own merged
     * predicate answers most cases, but walking the branches keeps a kind that only one branch
     * accepts. Returns an empty set for a cell that accepts no hatch (plain casing, air, or an
     * adder with no item filter).
     */
    Set<String> kindsAt(IStructureElement<Object> element, Object controller, World world, int x, int y, int z,
        ItemStack trigger) {
        Set<String> kinds = new LinkedHashSet<>();
        for (IStructureElement<Object> leaf : flatten(element)) {
            Predicate<ItemStack> accepts = predicateOf(leaf, controller, world, x, y, z, trigger);
            if (accepts == null) {
                continue;
            }
            for (Map.Entry<String, ItemStack> probe : probes.entrySet()) {
                if (kinds.contains(probe.getKey())) {
                    continue;
                }
                try {
                    if (accepts.test(probe.getValue())) {
                        kinds.add(probe.getKey());
                    }
                } catch (Exception | LinkageError e) {
                    // A predicate that trips on a probe tells us nothing about this kind; keep going
                    // rather than lose the kinds the other branches did answer.
                    LOG.debug("gtnh-extractor: hatch predicate threw for {} at {},{},{}", probe.getKey(), x, y, z);
                }
            }
        }
        List<String> ordered = new ArrayList<>(kinds);
        Collections.sort(ordered);
        return new LinkedHashSet<>(ordered);
    }

    /** {@code element} plus, when it is a chain, each of its fallback branches (one level deep). */
    @SuppressWarnings("unchecked")
    private static List<IStructureElement<Object>> flatten(IStructureElement<Object> element) {
        List<IStructureElement<Object>> out = new ArrayList<>();
        out.add(element);
        if (element instanceof IStructureElementChain) {
            IStructureElement<Object>[] fallbacks = ((IStructureElementChain<Object>) element).fallbacks();
            if (fallbacks != null) {
                for (IStructureElement<Object> fallback : fallbacks) {
                    if (fallback != null) {
                        out.add(fallback);
                    }
                }
            }
        }
        return out;
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
