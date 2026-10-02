package net.gtnhsolver.extractor;

import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.lang.reflect.ParameterizedType;
import java.lang.reflect.Type;
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
import gregtech.api.metatileentity.implementations.MTEHatchOutput;
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
 * Which layer a cell belongs to is the LAYER step's answer (below): a tower whose hatches sit on the
 * wrong layers sends each fluid to the wrong place, and one with a layer that has no output hatch
 * does not form at all.
 *
 * <p>
 * Neither of the two obvious routes works. The hint pass only yields a dot index, which is a
 * machine-local integer the structure's author chose (and 13/14/15 are StructureLib's reserved
 * AIR/NOT_AIR/ERROR markers, not hatch data). Re-running the block pass with hatches enabled does not
 * answer it either. Up to GT 5.09.54.20 a hatch element's {@code placeBlock} returned an unconditional
 * {@code false}, so {@code construct(...)} never placed a hatch at all; from 5.09.54.133 it places the
 * first hatch in GT's creative hatch source that its filter accepts, which names one kind the cell
 * takes, not the set of them. (The block pass now empties that source, see {@code StructureDumper}.)
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
 *
 *   then once per built form, over every cell those steps found takes an output hatch:
 *
 *   LAYER   : stand an output hatch in every such cell at once and run the machine's own checkMachine.
 *             A layered machine files each hatch in a per-layer list, read back by its type
 *             (List&lt;List&lt;MTEHatchOutput&gt;&gt;); the list a cell's hatch lands in is the
 *             output layer it feeds. A machine with no such list records no layers.
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
 * ends the tower at that layer (via {@code checkMachine}). That is not a slot of the form being dumped,
 * and the whole-machine check says so where the element's cannot. An output hatch in a Distillation
 * Tower's top centre is not such a change: it ends the tower just as the casing there does
 * ({@code onTopLayerFound}), so it is a slot of the form, though one that feeds no layer.
 *
 * <p>
 * Both probing steps ask a throwaway copy of the controller, never the built one: a check that passes
 * adds the hatch to the controller's hatch lists, and GT's filter hides a kind once the controller
 * holds one ({@code HatchElementBuilder.atLeast} filters on {@code count < limit}), so asking the built
 * controller would blind every filter probe after it. The element check runs once per element per
 * controller ({@link #forgetCheckedElements}), since what an adder accepts does not depend on the cell;
 * the whole-machine check runs per cell, against the shell's own verdict taken once per built form
 * ({@link #beginVariant}). The layer step asks a throwaway copy too, once per built form
 * ({@link #outputLayers}). Every probe restores the cell's block, and every probe is wrapped: a
 * throwing element, predicate or check degrades to "no kinds" (or "no layers", with a note saying
 * why), never to a failed dump.
 */
final class HatchProbe {

    private static final Logger LOG = LogManager.getLogger("gtnh-extractor");

    /** How deep a chain's branches are walked: StructureLib chains nest, but only a few levels. */
    private static final int MAX_CHAIN_DEPTH = 8;

    /** The kind whose cells the layer step fills, as a slot's {@code kinds} names it. */
    static final String OUTPUT_HATCH = HatchElement.OutputHatch.name();

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
    /** {@code IHatchElement.matchesHatch(mte)}, GT's own test of a hatch against a kind, or null before 5.09.54.133. */
    private static final Method MATCHES_HATCH = gtMethod(HatchElement.class, "matchesHatch", IMetaTileEntity.class);
    /** {@code IHatchElement.mteBlacklist()}, the hatch classes a kind turns away though they match it, or null. */
    private static final Method MTE_BLACKLIST = gtMethod(HatchElement.class, "mteBlacklist");

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

    /**
     * What the layer step found for one built form: the index of the per-layer list each probed cell's
     * hatch landed in, and why the answer cannot be trusted where it cannot.
     */
    static final class OutputLayers {

        /** Per probed cell, in the order they were given: its list index, or null for a cell in no list. */
        final Integer[] layers;
        /** The field the machine keeps its per-layer lists in, or null for a machine that keeps none. */
        String field;
        /** How many per-layer lists the machine's check filled. */
        int count;
        /** Why cells got no layer that should have one; empty when the answer stands as given. */
        final List<String> notes = new ArrayList<>();

        OutputLayers(int cells) {
            this.layers = new Integer[cells];
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
        // Which MTE stands for each kind, so a bump that hands two kinds one hatch shows in the log.
        List<String> chosen = new ArrayList<>();
        for (Map.Entry<String, Probe> probe : probes.entrySet()) {
            chosen.add(probe.getKey() + "=" + probe.getValue().id);
        }
        LOG.info("gtnh-extractor: hatch probe MTE per kind: {}", chosen);
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
     * A representative hatch for one kind: the first registered MTE that GT's own hatch filter would take
     * as that kind, which is what {@code HatchElementBuilder.anyOf} asks: {@code kind.matchesHatch(mte)},
     * and a class not in {@code kind.mteBlacklist()}. Declaration-driven, so a GT bump that renumbers
     * hatches is picked up automatically and only a kind GT stopped registering goes missing.
     *
     * <p>
     * "The first MTE of a class the kind declares" is not the same question from GT 5.09.54.133:
     * {@code CryotheumHatch} and {@code PyrotheumHatch} both declare {@code MTEHatchCustomFluidBase} and
     * differ only in {@code matchesHatch} (the fluid the hatch is locked to), so asking by class handed
     * both the Cryotheum hatch. Both methods are reached reflectively; on a GT without
     * {@code matchesHatch} the declared classes are asked instead, which is what its default does.
     */
    private static Probe findProbe(HatchElement kind, List<? extends Class<? extends IMetaTileEntity>> classes) {
        List<?> blacklist = blacklistOf(kind);
        IMetaTileEntity[] all = GregTechAPI.METATILEENTITIES;
        for (int id = 0; id < all.length; id++) {
            IMetaTileEntity mte = all[id];
            if (mte == null || blacklist.contains(mte.getClass()) || !matches(kind, classes, mte)) {
                continue;
            }
            ItemStack form = mte.getStackForm(1);
            if (form != null) {
                return new Probe(kind, form, mte, id);
            }
        }
        return null;
    }

    /** Whether GT takes {@code mte} as {@code kind}: its {@code matchesHatch}, else a declared class. */
    private static boolean matches(HatchElement kind, List<? extends Class<? extends IMetaTileEntity>> classes,
        IMetaTileEntity mte) {
        if (kind != null && MATCHES_HATCH != null) {
            try {
                return Boolean.TRUE.equals(MATCHES_HATCH.invoke(kind, mte));
            } catch (ReflectiveOperationException | RuntimeException | LinkageError e) {
                return false; // a kind that cannot be asked about this hatch does not take it
            }
        }
        for (Class<? extends IMetaTileEntity> cls : classes) {
            if (cls != null && cls.isInstance(mte)) {
                return true;
            }
        }
        return false;
    }

    /** The classes {@code kind} turns away (GT compares exact classes), or empty for none or no such API. */
    private static List<?> blacklistOf(HatchElement kind) {
        if (kind == null || MTE_BLACKLIST == null) {
            return Collections.emptyList();
        }
        try {
            Object blacklist = MTE_BLACKLIST.invoke(kind);
            return blacklist instanceof List ? (List<?>) blacklist : Collections.emptyList();
        } catch (ReflectiveOperationException | RuntimeException | LinkageError e) {
            return Collections.emptyList();
        }
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
     * The LAYER step: which of the machine's per-layer output lists each of {@code cells} feeds.
     *
     * <p>
     * A layered machine fills its output hatches by layer, not first fit: a Distillation Tower hands the
     * recipe's fluid output {@code i} to the hatches in {@code mOutputHatchesByLayer.get(i)} and no
     * others, and does not form at all while any layer has none. Which list a hatch goes in is the
     * machine's own bookkeeping (the height its check had climbed to when it met the cell, or a Mega
     * tower's five-high band), so the machine is asked: an output hatch stands in every cell at once, as
     * in a built tower with a hatch on each layer, and its own {@code checkMachine} runs on a throwaway
     * copy of the controller. The lists are then read back from the copy by their type, since their name
     * differs between machines ({@code outputHatchesPerLayer} on GT's Mega tower).
     *
     * <p>
     * Every cell is filled, not one at a time, because the check climbs layer by layer and stops at the
     * first layer it cannot pass, so a lone hatch would only ever be filed if it sat on the first layer.
     * The top centre is filled too: a hatch there ends the tower just as the casing does. Every cell's
     * block is put back afterwards. The answer is dropped, with a note, if it cannot be whole: a list
     * left with no probe hatch means the fill was incomplete (no layers for the form), a cell filed
     * under two lists gets no layer, and a check that throws gives no layers.
     */
    OutputLayers outputLayers(Object controller, World world, List<int[]> cells) {
        OutputLayers result = new OutputLayers(cells.size());
        Field field = layerListField(controller.getClass());
        Probe probe = probes.get(OUTPUT_HATCH);
        if (field == null || probe == null || cells.isEmpty()) {
            return result; // not a layered machine (or nothing to file): there are no layers to record
        }
        result.field = field.getName();
        Block[] originals = new Block[cells.size()];
        int[] originalMetas = new int[cells.size()];
        Map<Long, Integer> cellAt = new HashMap<>();
        try {
            for (int i = 0; i < cells.size(); i++) {
                int[] c = cells.get(i);
                Block original = world.getBlock(c[0], c[1], c[2]);
                int originalMeta = world.getBlockMetadata(c[0], c[1], c[2]);
                if (original == null || original.hasTileEntity(originalMeta)) {
                    continue; // never disturb a tile entity; an incomplete fill is reported below
                }
                originals[i] = original;
                originalMetas[i] = originalMeta;
                if (place(world, c[0], c[1], c[2], probe)) {
                    cellAt.put(cellKey(c[0], c[1], c[2]), i);
                }
            }
            List<List<?>> lists = new ArrayList<>();
            String[] thrown = new String[1];
            boolean asked = withScratch(controller, scratch -> {
                try {
                    scratch.clearHatches();
                    if (CHECK_WITH_ERRORS != null) {
                        CHECK_WITH_ERRORS.invoke(scratch, scratch.getBaseMetaTileEntity(), null, new ArrayList<>());
                    } else {
                        scratch.checkMachine(scratch.getBaseMetaTileEntity(), null);
                    }
                    for (Object list : (List<?>) field.get(scratch)) {
                        lists.add((List<?>) list);
                    }
                    return true;
                } catch (Exception | LinkageError e) {
                    Throwable cause = e instanceof java.lang.reflect.InvocationTargetException ? e.getCause() : e;
                    thrown[0] = String.valueOf(cause);
                    return false;
                }
            });
            if (!asked) {
                result.notes.add(
                    "output layers not recorded: the structure check could not be run on a copy of the controller"
                        + (thrown[0] != null ? " (" + thrown[0] + ")" : ""));
                return result;
            }
            fileLayers(lists, cellAt, result);
        } finally {
            for (int i = 0; i < cells.size(); i++) {
                if (originals[i] != null) {
                    int[] c = cells.get(i);
                    world.setBlock(c[0], c[1], c[2], originals[i], originalMetas[i], 2);
                }
            }
        }
        return result;
    }

    /**
     * File each probe hatch the check put in {@code lists} under its list's index, applying the layer
     * step's rules: a cell in two lists gets none, and a list holding no probe hatch voids them all.
     */
    private static void fileLayers(List<List<?>> lists, Map<Long, Integer> cellAt, OutputLayers result) {
        result.count = lists.size();
        if (lists.isEmpty()) {
            result.notes.add("output layers not recorded: the structure check filed no hatch under any layer");
            return;
        }
        Set<Integer> twice = new TreeSet<>();
        List<Integer> empty = new ArrayList<>();
        for (int layer = 0; layer < lists.size(); layer++) {
            boolean filed = false;
            for (Object hatch : lists.get(layer)) {
                Integer cell = hatch instanceof IMetaTileEntity ? cellOf((IMetaTileEntity) hatch, cellAt) : null;
                if (cell == null) {
                    continue;
                }
                filed = true;
                Integer before = result.layers[cell];
                if (before != null && before != layer) {
                    twice.add(cell);
                }
                result.layers[cell] = layer;
            }
            if (!filed) {
                empty.add(layer);
            }
        }
        if (!empty.isEmpty()) {
            java.util.Arrays.fill(result.layers, null);
            result.notes.add(
                "output layers not recorded: the structure check filed no probe hatch under layer(s) " + empty
                    + " of " + lists.size() + ", so the fill was incomplete");
            return;
        }
        for (int cell : twice) {
            result.layers[cell] = null;
        }
        if (!twice.isEmpty()) {
            result.notes.add(
                "output layer not recorded for " + twice.size()
                    + " cell(s) the structure check filed under two layers");
        }
    }

    /** Which probed cell {@code hatch} stands in, or null for a hatch the probe did not place. */
    private static Integer cellOf(IMetaTileEntity hatch, Map<Long, Integer> cellAt) {
        IGregTechTileEntity base = hatch.getBaseMetaTileEntity();
        return base == null ? null : cellAt.get(cellKey(base.getXCoord(), base.getYCoord(), base.getZCoord()));
    }

    private static long cellKey(int x, int y, int z) {
        return ((long) (x & 0x1FFFFF) << 42) | ((long) (y & 0x1FFFFF) << 21) | (z & 0x1FFFFF);
    }

    /**
     * The field a layered machine keeps its output hatches by layer in, found by its type,
     * {@code List<List<T extends MTEHatchOutput>>}, walking up from the controller's own class. Null for
     * a machine that keeps none.
     */
    private static Field layerListField(Class<?> type) {
        for (Class<?> c = type; c != null && c != Object.class; c = c.getSuperclass()) {
            for (Field field : c.getDeclaredFields()) {
                Type element = listElement(field.getGenericType());
                Type hatch = element == null ? null : listElement(element);
                if (hatch instanceof Class && MTEHatchOutput.class.isAssignableFrom((Class<?>) hatch)) {
                    try {
                        field.setAccessible(true);
                        return field;
                    } catch (SecurityException e) {
                        return null;
                    }
                }
            }
        }
        return null;
    }

    /** The element type of a parameterized {@code List} type, or null if {@code type} is not one. */
    private static Type listElement(Type type) {
        if (!(type instanceof ParameterizedType)) {
            return null;
        }
        ParameterizedType parameterized = (ParameterizedType) type;
        Type raw = parameterized.getRawType();
        Type[] arguments = parameterized.getActualTypeArguments();
        return raw instanceof Class && List.class.isAssignableFrom((Class<?>) raw) && arguments.length == 1
            ? arguments[0]
            : null;
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
