package net.gtnhsolver.extractor;

import java.util.ArrayList;
import java.util.List;

import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

/**
 * Collects per-controller failures so one broken multiblock never kills the run (design
 * principle 5 of {@code docs/dataset-extraction/plan.md}: "fail loud, per-controller"). Every entry
 * lands in {@code _meta.json.failures} as {@code {registry_name, reason}}, turning a version bump
 * that breaks extraction of some multiblock into a visible coverage regression in the PR diff
 * rather than a silent absence.
 *
 * <p>
 * Reasons captured: a {@link Throwable} escaping {@code construct}/placement, a non-terminating
 * or explosive trigger-stack sweep (hard-capped), and an empty scan (the controller built nothing
 * in a void world).
 */
final class ErrorCollector {

    private static final Logger LOG = LogManager.getLogger(DumperMod.MODID);

    private final List<DumpModel.Failure> failures = new ArrayList<>();

    /** Record a failed controller. {@code registryName} identifies it; {@code reason} is one line. */
    void record(String registryName, String reason) {
        failures.add(new DumpModel.Failure(registryName, reason));
        LOG.warn("gtnh-extractor: skipped {} - {}", registryName, reason);
    }

    /** Record a failure from a caught throwable, flattening it to a compact {@code type: message}. */
    void record(String registryName, Throwable t) {
        String message = t.getMessage();
        String reason = t.getClass()
            .getSimpleName() + (message != null ? ": " + message : "");
        record(registryName, reason);
    }

    List<DumpModel.Failure> failures() {
        return failures;
    }

    int count() {
        return failures.size();
    }
}
