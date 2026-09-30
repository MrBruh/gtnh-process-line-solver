package net.gtnhsolver.extractor;

import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
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
import gregtech.api.interfaces.tileentity.IGregTechTileEntity;
import gregtech.api.metatileentity.BaseMetaTileEntity;
import gregtech.api.metatileentity.implementations.MTEBasicHull;
import gregtech.api.metatileentity.implementations.MTEMultiBlockBase;

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
 * So we ask GT itself, in up to three steps:
 *
 * <pre>
 *   each leaf of the visited element (a chain is walked into its branches)
 *        |
 *        v
 *   FILTER  : getBlocksToPlace(...).getPredicate(), tested with one probe stack per HatchElement kind.
 *        |    On a freshly placed controller every hatch count is zero, so this is the element's
 *        |    MAXIMAL legal set. Most GT hatch elements (HatchElementBuilder) answer here, and what
 *        |    they answer is recorded as it stands.
 *        v    names no kind?
 *   CHECK   : stand each probe hatch in the cell as a real GT tile and ask the element's own check().
 *        |    A hatch adder built from a bare method reference (GTStructureUtility.ofHatchAdder) has
 *        |    no filter, and only answers here: the Distillation Tower's ring takes energy hatches
 *        |    this way (GitHub #227). A kind is a candidate only if the hatch then counts as that
 *        |    kind (HatchElement.count), and never if the element takes a plain machine hull too.
 *        v    candidates the filter did not already give this cell
 *   CONFIRM : the machine's own checkMachine over the whole built shell, with the probe hatch in the
 *             cell. Recorded only if the hatch counts as the kind AND the check reports no kind of
 *             error the bare shell did not already have.
 * </pre>
 *
 * <p>
 * The confirm step needs GT's structure-error API ({@code gregtech.api.structure.error} and the
 * {@code checkMachine} overload that fills a list of errors), which only GT 5.09.54 (GTNH 2.9) has, so it
 * is reached reflectively and the extractor still compiles against 2.8.4's GT (#249). Without it a
 * pass or fail cannot tell a new error from the one the bare shell already has, so the step abstains:
 * a bare adder's kinds are then not recorded, which under-reports a cell (the dataset's kinds are a
 * lower bound) rather than claiming one takes a hatch it refuses.
 *
 * <p>
 * The confirm step is what keeps a bare adder from over-reporting. An element's check accepts a hatch
 * that the machine as a whole then reads as a change of shape: a muffler on a Dangote Distillus ring
 * ends the tower at that layer, and an output hatch in a Distillation Tower's top centre tells it the
 * tower goes on (both via {@code checkMachine}). Neither is a slot of the form being dumped, and the
 * whole-machine check says so where the element's cannot.
 *
 * <p>
 * Both probing steps ask a throwaway copy of the controller, never the built one: a check that passes
 * adds the hatch to the controller's hatch lists, and GT's filter hides a kind once the controller
 * holds one ({@code HatchElementBuilder.atLeast} filters on {@code count < limit}), so asking the built
 * controller would blind every filter probe after it. The element check runs once per element per
 * controller ({@link #forgetCheckedElements}), since what an adder accepts does not depend on the cell;
 * the whole-machine check runs per cell, against the shell's own verdict taken once per built form
 * ({@link #beginVariant}). Every probe restores the cell's block, and every probe is wrapped: a
 * throwing element, predicate or check degrades to "no kinds", never to a failed dump.
 */
final class HatchProbe {

    private static final Logger LOG = LogManager.getLogger("gtnh-extractor");

    /** How deep a chain's branches are walked: StructureLib chains nest, but only a few levels. */
    private static final int MAX_CHAIN_DEPTH = 8;

    /** GT's structure errors, or null on a GT without them (2.8.4); see the class comment. */
    private static final Class<?> STRUCTURE_ERROR = gtClass("gregtech.api.structure.error.StructureError");
    private static final Class<?> TRANSLATABLE_ERROR = gtClass(
        "gregtech.api.structure.error.TranslatableStructureError");
    /** {@code checkMachine(base, stack, errors)}, the whole-machine check that lists its errors. */
    private static final Method CHECK_WITH_ERRORS = gtMethod(
        MTEMultiBlockBase.class,
        "checkMachine",
        IGregTechTileEntity.class,
        ItemStack.class,
        List.class);
    /** Resolved on the public interface and record, since GT's other error classes are not public. */
    private static final Method ERROR_ID = gtMethod(STRUCTURE_ERROR, "getId");
    private static final Method ERROR_MESSAGE = gtMethod(TRANSLATABLE_ERROR, "message");

    /** A hatch to probe with: its kind, its item form for the filter, and its MTE for placing one. */
    private static final class Probe {

        /** Null for the machine-hull control, which stands for no kind. */
        final HatchElement kind;
        final ItemStack stack;
        final IMetaTileEntity mte;
        final int id;

        Probe(HatchElement kind, ItemStack stack, IMetaTileEntity mte, int id) {
            this.kind = kind;
            this.stack = stack;
            this.mte = mte;
            this.id = id;
        }
    }

    /** What a machine's own structure check said: the kinds of error, and how many of each hatch. */
    private static final class Verdict {

        final Set<String> errors;
        final Map<HatchElement, Long> counts;

        Verdict(Set<String> errors, Map<HatchElement, Long> counts) {
            this.errors = errors;
            this.counts = counts;
        }
    }

    /** One probe per hatch kind, in {@link HatchElement} declaration order. */
    private final Map<String, Probe> probes = new LinkedHashMap<>();
    /** The element check's control: a machine hull, which no hatch adder takes. Null if GT has none. */
    private final Probe hull;
    /** What each element's check took, per controller, by element identity. */
    private final Map<Object, Set<String>> checked = new IdentityHashMap<>();
    /** The built form's own verdict, taken lazily once per form; null until then, or if it cannot be. */
    private Verdict shell;
    private boolean shellJudged;
    /** Candidates the whole-machine check kept and turned down, over the dump, for the run's log. */
    private int confirmed;
    private int refused;

    HatchProbe() {
        for (HatchElement kind : HatchElement.values()) {
            List<? extends Class<? extends IMetaTileEntity>> classes = kind.mteClasses();
            Probe probe = classes == null ? null : findProbe(kind, classes);
            if (probe != null) {
                probes.put(kind.name(), probe);
            }
        }
        hull = findProbe(null, Collections.singletonList(MTEBasicHull.class));
        LOG.info(
            "gtnh-extractor: hatch probe built for {} of {} kinds (element-check control: {}; confirm: {})",
            probes.size(),
            HatchElement.values().length,
            hull != null ? "machine hull" : "none, so bare adders go unprobed",
            CHECK_WITH_ERRORS != null && ERROR_ID != null ? "checkMachine errors"
                : "none, GT has no structure errors, so bare adders are not recorded");
    }

    /** A GT class by name, or null if this GT does not have it. */
    private static Class<?> gtClass(String name) {
        try {
            return Class.forName(name, false, HatchProbe.class.getClassLoader());
        } catch (ClassNotFoundException | LinkageError e) {
            return null;
        }
    }

    /** A public method of {@code owner}, or null if {@code owner} is null or this GT lacks the method. */
    private static Method gtMethod(Class<?> owner, String name, Class<?>... parameters) {
        if (owner == null) {
            return null;
        }
        try {
            return owner.getMethod(name, parameters);
        } catch (NoSuchMethodException | SecurityException e) {
            return null;
        }
    }

    /**
     * A representative hatch for one kind: the first registered MTE assignable to one of the classes the
     * kind declares. Declaration-driven, so a GT bump that renumbers hatches is picked up automatically
     * and only a kind GT stopped registering goes missing.
     */
    private static Probe findProbe(HatchElement kind, List<? extends Class<? extends IMetaTileEntity>> classes) {
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
                        return new Probe(kind, form, mte, id);
                    }
                }
            }
        }
        return null;
    }

    /** Drop what the element checks answered: a new controller's elements are asked afresh. */
    void forgetCheckedElements() {
        checked.clear();
    }

    /** A new form is about to be probed: its shell's own verdict is taken afresh when first needed. */
    void beginVariant() {
        shell = null;
        shellJudged = false;
    }

    /** How the whole-machine check ruled over the dump so far, for the run's log. */
    String summary() {
        return confirmed + " bare-adder kind(s) confirmed by checkMachine, " + refused + " refused";
    }

    /**
     * The hatch kinds {@code element} accepts at {@code (x,y,z)}, sorted for a stable dump. Empty for
     * a cell that accepts no hatch (plain casing, air, or an element that takes any GT tile).
     */
    Set<String> kindsAt(IStructureElement<Object> element, Object controller, World world, int x, int y, int z,
        ItemStack trigger) {
        Set<String> kinds = new LinkedHashSet<>();
        Set<String> candidates = new LinkedHashSet<>();
        for (IStructureElement<Object> leaf : flatten(element)) {
            Set<String> filtered = filterKinds(leaf, controller, world, x, y, z, trigger);
            if (filtered.isEmpty()) {
                candidates.addAll(checkedKinds(leaf, controller, world, x, y, z));
            } else {
                kinds.addAll(filtered);
            }
        }
        candidates.removeAll(kinds);
        for (String kind : candidates) {
            if (confirm(probes.get(kind), controller, world, x, y, z)) {
                kinds.add(kind);
                confirmed++;
            } else {
                refused++;
            }
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
     * The candidate kinds {@code leaf}'s own check accepts, asked by standing each probe hatch in the
     * cell (the CHECK step). Memoised per element for the current controller. A cell holding a tile
     * entity is not disturbed: the element is asked at the next cell it governs instead.
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
            if (!elementTakes(leaf, controller, world, x, y, z, hull)) {
                for (Map.Entry<String, Probe> probe : probes.entrySet()) {
                    if (elementTakes(leaf, controller, world, x, y, z, probe.getValue())) {
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

    /**
     * Whether {@code leaf}'s check passes with {@code probe}'s hatch standing at the cell, and the hatch
     * then counts as its kind (the hull control has no kind: its check passing is the whole question).
     */
    private static boolean elementTakes(IStructureElement<Object> leaf, Object controller, World world, int x, int y,
        int z, Probe probe) {
        return withScratch(controller, scratch -> {
            if (!place(world, x, y, z, probe) || !leaf.check(scratch, world, x, y, z)) {
                return false;
            }
            return probe.kind == null || probe.kind.count(scratch) > 0;
        });
    }

    /**
     * The CONFIRM step: whether the machine's own {@code checkMachine}, run over the whole built form
     * with {@code probe}'s hatch in the cell, counts the hatch as its kind and reports no kind of error
     * the bare shell does not.
     */
    private boolean confirm(Probe probe, Object controller, World world, int x, int y, int z) {
        if (!shellJudged) {
            shell = judge(controller);
            shellJudged = true;
        }
        if (shell == null || probe == null) {
            return false; // the machine cannot be asked, so a bare adder's word is not taken alone
        }
        Block original = world.getBlock(x, y, z);
        int originalMeta = world.getBlockMetadata(x, y, z);
        if (original == null || original.hasTileEntity(originalMeta)) {
            return false;
        }
        try {
            if (!place(world, x, y, z, probe)) {
                return false;
            }
            Verdict with = judge(controller);
            return with != null && shell.errors.containsAll(with.errors)
                && with.counts.get(probe.kind) > shell.counts.get(probe.kind);
        } finally {
            world.setBlock(x, y, z, original, originalMeta, 2);
        }
    }

    /**
     * What the machine's own structure check says about the world as it stands, or {@code null} if it
     * cannot be asked (a controller that is not a GT multiblock base, a check that throws, or a GT
     * without structure errors, whose bare pass or fail cannot tell a new error from an old one).
     */
    private Verdict judge(Object controller) {
        if (CHECK_WITH_ERRORS == null || ERROR_ID == null) {
            return null;
        }
        Verdict[] verdict = new Verdict[1];
        withScratch(controller, scratch -> {
            scratch.clearHatches();
            List<Object> errors = new ArrayList<>();
            CHECK_WITH_ERRORS.invoke(scratch, scratch.getBaseMetaTileEntity(), null, errors);
            Set<String> kinds = new TreeSet<>();
            for (Object error : errors) {
                kinds.add(errorKind(error));
            }
            Map<HatchElement, Long> counts = new HashMap<>();
            for (Probe probe : probes.values()) {
                counts.put(probe.kind, probe.kind.count(scratch));
            }
            verdict[0] = new Verdict(kinds, counts);
            return true;
        });
        return verdict[0];
    }

    /**
     * An error's kind without its figures: its id, plus the lang key for GT's translatable errors, which
     * all share one id. "Missing hatch: Energy" and "missing hatch: Maintenance" are one kind, so fixing
     * one while the other stands is not a new error, and "layers 2, 3 lack an output hatch" becoming
     * "layer 3 lacks one" is not either.
     */
    private static String errorKind(Object error) {
        String kind;
        Object text;
        try {
            kind = String.valueOf(ERROR_ID.invoke(error));
            if (ERROR_MESSAGE == null || !TRANSLATABLE_ERROR.isInstance(error)) {
                return kind;
            }
            text = ERROR_MESSAGE.invoke(error);
        } catch (ReflectiveOperationException | RuntimeException e) {
            return error.getClass()
                .getName();
        }
        try {
            // LangText is package-private in GT, so its record accessor is reached reflectively.
            Method key = text.getClass()
                .getDeclaredMethod("key");
            key.setAccessible(true);
            return kind + ":" + key.invoke(text);
        } catch (ReflectiveOperationException | RuntimeException e) {
            return kind + ":" + text.getClass()
                .getSimpleName();
        }
    }

    /** A question put to a throwaway copy of the controller. */
    private interface ScratchQuery {

        boolean ask(MTEMultiBlockBase scratch) throws Exception;
    }

    /**
     * Ask {@code query} of a fresh copy of the built controller, attached to the same base tile and facing
     * the same way, then put the built controller back on its base. False if there is no copy to ask
     * (not a GT multiblock) or the question throws.
     */
    private static boolean withScratch(Object controller, ScratchQuery query) {
        if (!(controller instanceof MTEMultiBlockBase)) {
            return false;
        }
        MTEMultiBlockBase built = (MTEMultiBlockBase) controller;
        IGregTechTileEntity base = built.getBaseMetaTileEntity();
        if (base == null) {
            return false;
        }
        try {
            IMetaTileEntity copy = built.newMetaEntity(base);
            if (!(copy instanceof MTEMultiBlockBase)) {
                return false;
            }
            MTEMultiBlockBase scratch = (MTEMultiBlockBase) copy;
            copyFacing(built, scratch);
            scratch.setBaseMetaTileEntity(base);
            return query.ask(scratch);
        } catch (Exception | LinkageError e) {
            return false; // an adder or check that throws on a stranger's hatch does not take it
        } finally {
            built.setBaseMetaTileEntity(base);
        }
    }

    /** Give {@code scratch} the built controller's facing, which its structure check reads. */
    private static void copyFacing(Object built, Object scratch) {
        for (Class<?> c = built.getClass(); c != null && c != Object.class; c = c.getSuperclass()) {
            try {
                Field field = c.getDeclaredField("mExtendedFacing");
                field.setAccessible(true);
                field.set(scratch, field.get(built));
                return;
            } catch (NoSuchFieldException ignored) {
                // keep walking up the hierarchy
            } catch (IllegalAccessException e) {
                return;
            }
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
