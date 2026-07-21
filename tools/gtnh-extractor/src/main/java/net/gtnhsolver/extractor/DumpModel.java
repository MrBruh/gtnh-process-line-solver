package net.gtnhsolver.extractor;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Plain data holders mirroring the schema-v1 dataset contract (one file per controller plus a
 * run summary). These carry <em>raw facts only</em> - block registry names, metas, relative
 * offsets, hint positions - exactly as scanned from the world. All interpretation (footprint
 * math, I/O faces, tier semantics) lives in the Python adapter, per design principle 3 of
 * {@code docs/dataset-extraction/plan.md}; nothing here decides anything solver-shaped.
 *
 * <p>
 * The field names and nesting match {@code src/gtnh_solver/dataset/schema.py} so that
 * {@link JsonWriter}'s serialisation validates against the Pydantic loader. Offsets are
 * {@code [dx, dy, dz]} world-space deltas from the controller block (see
 * {@link Controller#facingConvention}); the Python adapter re-derives the bounding box from the
 * block span and cross-checks it against {@link Variant#bbox}, so the two must agree.
 */
final class DumpModel {

    private DumpModel() {}

    /** Identity of the controller a file describes (schema {@code controller} object). */
    static final class Controller {

        final String registryName;
        final int meta;
        final String displayName;
        final String sourceClass;
        final String facingConvention;

        Controller(String registryName, int meta, String displayName, String sourceClass, String facingConvention) {
            this.registryName = registryName;
            this.meta = meta;
            this.displayName = displayName;
            this.sourceClass = sourceClass;
            this.facingConvention = facingConvention;
        }
    }

    /** One placed block in a variant: its controller-relative offset and identity. */
    static final class PlacedBlock {

        final int dx;
        final int dy;
        final int dz;
        final String block;
        final int meta;

        PlacedBlock(int dx, int dy, int dz, String block, int meta) {
            this.dx = dx;
            this.dy = dy;
            this.dz = dz;
            this.block = block;
            this.meta = meta;
        }
    }

    /** One hint-dot position: a legal hatch / degree-of-freedom slot the projector shows. */
    static final class HintDot {

        final int dx;
        final int dy;
        final int dz;
        final int hint;

        HintDot(int dx, int dy, int dz, int hint) {
            this.dx = dx;
            this.dy = dy;
            this.dz = dz;
            this.hint = hint;
        }
    }

    /** One distinct built form of a controller (a trigger-stack / channel selection). */
    static final class Variant {

        final int triggerStackSize;
        final Map<String, Integer> channels = new LinkedHashMap<>();
        final List<PlacedBlock> blocks = new ArrayList<>();
        final List<HintDot> hints = new ArrayList<>();
        int[] bbox = new int[] { 0, 0, 0 };

        Variant(int triggerStackSize) {
            this.triggerStackSize = triggerStackSize;
        }
    }

    /**
     * One identity-only channel alternative: a tiered block a channel value swaps in without changing
     * the structure's shape (a coil/glass/pipe-casing tier). Recorded once per controller in the
     * {@code substitutions} table keyed by channel, so a 14-tier coil is one shape variant plus a
     * substitution list rather than 14 exploded variants.
     */
    static final class Substitution {

        final int channelValue;
        final String block;
        final int meta;

        Substitution(int channelValue, String block, int meta) {
            this.channelValue = channelValue;
            this.block = block;
            this.meta = meta;
        }
    }

    /** A whole {@code data/multiblocks/<name>.json} file: one controller, its variants, and subs. */
    static final class MultiblockDoc {

        final Controller controller;
        final List<Variant> variants = new ArrayList<>();
        /** Identity-only channel effects keyed by channel name (e.g. {@code "coil"}); may be empty. */
        final Map<String, List<Substitution>> substitutions = new LinkedHashMap<>();
        /**
         * Caveats about THIS controller's dump that a consumer must not mistake for completeness -
         * chiefly a variant family larger than the trigger-stack sweep could reach. The doc is still
         * emitted (a truncated family is useful; a missing controller is not), so the note is the
         * only thing standing between a partial dump and a consumer that believes it is total.
         */
        final List<String> failures = new ArrayList<>();

        MultiblockDoc(Controller controller) {
            this.controller = controller;
        }
    }

    /** One controller the extractor could not dump, for the {@code _meta.json} failure list. */
    static final class Failure {

        final String registryName;
        final String reason;

        Failure(String registryName, String reason) {
            this.registryName = registryName;
            this.reason = reason;
        }
    }
}
