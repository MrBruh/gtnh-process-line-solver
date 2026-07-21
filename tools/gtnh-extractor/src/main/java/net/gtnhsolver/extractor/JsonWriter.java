package net.gtnhsolver.extractor;

import java.io.File;
import java.io.IOException;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.Comparator;
import java.util.List;
import java.util.Map;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonPrimitive;

/**
 * Serialises the dumped facts to schema-v1 JSON with Gson (already on the 1.7.10 classpath). One
 * file per controller plus a {@code _meta.json} run summary, both under {@code <out>/multiblocks/}.
 *
 * <p>
 * Every list is sorted and every object's keys are emitted in a fixed order (variants by trigger
 * stack size; blocks and hints by {@code (dy, dz, dx)} then identity), so a dataset regenerated
 * after a pack bump produces a <em>minimal, reviewable diff</em> rather than a reshuffle. Field
 * order and nesting mirror {@code src/gtnh_solver/dataset/schema.py} exactly, so the output loads
 * through the Pydantic {@code extra="forbid"} loader without a translation step.
 *
 * <p>
 * Filenames sanitise the registry name (which contains {@code :} and {@code .}, illegal on some
 * filesystems) and append the meta for uniqueness, since many controllers share one block. The
 * Python loader keys machines by {@code display_name} from the file body, not the filename, so the
 * scheme is free to be filesystem-safe.
 */
final class JsonWriter {

    /**
     * Dataset schema version emitted; must match {@code SCHEMA_VERSION} in {@code schema.py}.
     *
     * <p>
     * v2 added {@code variants[].hatch_slots}. The Python models are {@code extra="forbid"}, so an
     * old loader rejects a new file outright rather than quietly ignoring the field - which is why
     * this is a bump rather than an additive no-op.
     */
    static final int SCHEMA_VERSION = 2;

    private static final Comparator<DumpModel.PlacedBlock> BLOCK_ORDER = Comparator
        .comparingInt((DumpModel.PlacedBlock b) -> b.dy)
        .thenComparingInt(b -> b.dz)
        .thenComparingInt(b -> b.dx)
        .thenComparing(b -> b.block)
        .thenComparingInt(b -> b.meta);

    private static final Comparator<DumpModel.HatchSlot> HATCH_SLOT_ORDER = Comparator
        .comparingInt((DumpModel.HatchSlot s) -> s.dy)
        .thenComparingInt(s -> s.dz)
        .thenComparingInt(s -> s.dx);

    private static final Comparator<DumpModel.HintDot> HINT_ORDER = Comparator
        .comparingInt((DumpModel.HintDot h) -> h.dy)
        .thenComparingInt(h -> h.dz)
        .thenComparingInt(h -> h.dx)
        .thenComparingInt(h -> h.hint);

    private static final Comparator<DumpModel.Substitution> SUB_ORDER = Comparator
        .comparingInt((DumpModel.Substitution s) -> s.channelValue)
        .thenComparing(s -> s.block)
        .thenComparingInt(s -> s.meta);

    private final Gson gson = new GsonBuilder().setPrettyPrinting()
        .disableHtmlEscaping()
        .create();

    /** Write one controller document to {@code <out>/multiblocks/<safe-name>.json}. */
    void writeDoc(File multiblocksDir, DumpModel.MultiblockDoc doc) throws IOException {
        JsonObject root = new JsonObject();
        root.addProperty("schema", SCHEMA_VERSION);
        root.add("controller", controllerJson(doc.controller));

        JsonArray variants = new JsonArray();
        doc.variants.stream()
            .sorted(Comparator.comparingInt(v -> v.triggerStackSize))
            .forEach(v -> variants.add(variantJson(v)));
        root.add("variants", variants);

        // Lane 3 (channel handling) fills the identity-substitution table: channels that only swap a
        // tiered block (coil, glass, ...) without changing the shape, keyed by channel name. Keys and
        // entries are sorted so a regenerated dataset diffs minimally rather than reshuffling.
        root.add("substitutions", substitutionsJson(doc.substitutions));

        // Per-doc caveats (e.g. a variant family the stack sweep could not exhaust). Sorted so a
        // regenerated dataset diffs minimally, matching the substitution table above.
        JsonArray failures = new JsonArray();
        doc.failures.stream()
            .sorted()
            .forEach(f -> failures.add(new JsonPrimitive(f)));
        root.add("failures", failures);

        write(new File(multiblocksDir, fileName(doc.controller)), root);
    }

    /** Write the run summary to {@code <out>/multiblocks/_meta.json}. */
    void writeMeta(File multiblocksDir, String packVersion, Map<String, String> modVersions, String generatedAt,
        String extractorSha, int controllerCount, List<DumpModel.Failure> failures) throws IOException {
        JsonObject root = new JsonObject();
        root.addProperty("schema", SCHEMA_VERSION);
        root.addProperty("pack_version", packVersion);

        JsonObject mods = new JsonObject();
        modVersions.forEach(mods::addProperty);
        root.add("mod_versions", mods);

        root.addProperty("generated_at", generatedAt);
        root.addProperty("extractor_sha", extractorSha);
        root.addProperty("controller_count", controllerCount);

        JsonArray failureArray = new JsonArray();
        failures.stream()
            .sorted(
                Comparator.comparing((DumpModel.Failure f) -> f.registryName)
                    .thenComparing(f -> f.reason))
            .forEach(f -> {
                JsonObject fj = new JsonObject();
                fj.addProperty("registry_name", f.registryName);
                fj.addProperty("reason", f.reason);
                failureArray.add(fj);
            });
        root.add("failures", failureArray);

        write(new File(multiblocksDir, "_meta.json"), root);
    }

    private JsonObject controllerJson(DumpModel.Controller c) {
        JsonObject o = new JsonObject();
        o.addProperty("registry_name", c.registryName);
        o.addProperty("meta", c.meta);
        o.addProperty("display_name", c.displayName);
        o.addProperty("source_class", c.sourceClass);
        o.addProperty("facing_convention", c.facingConvention);
        return o;
    }

    private JsonObject variantJson(DumpModel.Variant v) {
        JsonObject o = new JsonObject();
        o.addProperty("trigger_stack_size", v.triggerStackSize);

        JsonObject channels = new JsonObject();
        v.channels.forEach(channels::addProperty);
        o.add("channels", channels);

        JsonArray blocks = new JsonArray();
        v.blocks.stream()
            .sorted(BLOCK_ORDER)
            .forEach(b -> {
                JsonObject bj = new JsonObject();
                bj.add("d", offset(b.dx, b.dy, b.dz));
                bj.addProperty("block", b.block);
                bj.addProperty("meta", b.meta);
                blocks.add(bj);
            });
        o.add("blocks", blocks);

        JsonArray hints = new JsonArray();
        v.hints.stream()
            .sorted(HINT_ORDER)
            .forEach(h -> {
                JsonObject hj = new JsonObject();
                hj.add("d", offset(h.dx, h.dy, h.dz));
                hj.addProperty("hint", h.hint);
                hints.add(hj);
            });
        o.add("hints", hints);

        // Cells that accept a hatch, with the kinds each accepts. Sorted on the same (y, z, x) key as
        // blocks and hints so a regenerated dataset diffs minimally.
        JsonArray hatchSlots = new JsonArray();
        v.hatchSlots.stream()
            .sorted(HATCH_SLOT_ORDER)
            .forEach(s -> {
                JsonObject sj = new JsonObject();
                sj.add("d", offset(s.dx, s.dy, s.dz));
                JsonArray kinds = new JsonArray();
                s.kinds.forEach(k -> kinds.add(new JsonPrimitive(k)));
                sj.add("kinds", kinds);
                hatchSlots.add(sj);
            });
        o.add("hatch_slots", hatchSlots);

        o.add("bbox", offset(v.bbox[0], v.bbox[1], v.bbox[2]));
        return o;
    }

    /** Serialise the identity-substitution table: {@code {channel: [{channel_value, block, meta}]}}. */
    private JsonObject substitutionsJson(Map<String, List<DumpModel.Substitution>> substitutions) {
        JsonObject subs = new JsonObject();
        substitutions.entrySet()
            .stream()
            .sorted(Map.Entry.comparingByKey())
            .forEach(entry -> {
                JsonArray arr = new JsonArray();
                entry.getValue()
                    .stream()
                    .sorted(SUB_ORDER)
                    .forEach(s -> {
                        JsonObject sj = new JsonObject();
                        sj.addProperty("channel_value", s.channelValue);
                        sj.addProperty("block", s.block);
                        sj.addProperty("meta", s.meta);
                        arr.add(sj);
                    });
                subs.add(entry.getKey(), arr);
            });
        return subs;
    }

    // The Minecraft 1.7.10 Gson predates JsonArray's primitive add overloads, so wrap each int.
    private static JsonArray offset(int a0, int a1, int a2) {
        JsonArray a = new JsonArray();
        a.add(new JsonPrimitive(Integer.valueOf(a0)));
        a.add(new JsonPrimitive(Integer.valueOf(a1)));
        a.add(new JsonPrimitive(Integer.valueOf(a2)));
        return a;
    }

    private void write(File file, JsonObject root) throws IOException {
        file.getParentFile()
            .mkdirs();
        try (Writer w = Files.newBufferedWriter(file.toPath(), StandardCharsets.UTF_8)) {
            gson.toJson(root, w);
            w.write('\n');
        }
    }

    private static String fileName(DumpModel.Controller c) {
        String safe = c.registryName.replaceAll("[^A-Za-z0-9]+", "_");
        return safe + "_" + c.meta + ".json";
    }
}
