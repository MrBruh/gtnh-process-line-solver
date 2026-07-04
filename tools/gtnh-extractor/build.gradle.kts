
plugins {
    id("com.gtnewhorizons.gtnhconvention")
}

// Forward the dataset-run properties the CI (and the boot-verify command) pass on the Gradle
// command line into the forked `runServer` JVM as system properties, where DumperMod reads them.
//   -PdatasetOut=<dir>     where to emit <dir>/multiblocks/ (resolved against this project dir)
//   -PtextureOut=<dir>     where to emit <dir>/manifest.json (lane 6, texture pass; texture-only
//                          when -PdatasetOut is absent, so the run skips the structure dump)
//   -PpackVersion=<ver>    the GTNH pack release the dump tracks (recorded in _meta.json)
//   -PextractorSha=<sha>   git SHA of the extractor that produced the dump
//   -PmodVersions=<a=1,b=2> pinned tracked-mod versions from gtnh.lock.json (provenance; the
//                          runtime Forge container is the fallback and GT5U's reports "MC1710")
// The RFG run tasks are JavaExec-based; guard the cast so a future task-type change fails clearly.
tasks.matching { it.name == "runServer" }.configureEach {
    if (this is JavaExec) {
        (project.findProperty("datasetOut") as String?)?.let {
            systemProperty("gtnhextractor.datasetOut", project.file(it).absolutePath)
        }
        (project.findProperty("textureOut") as String?)?.let {
            systemProperty("gtnhextractor.textureOut", project.file(it).absolutePath)
        }
        (project.findProperty("packVersion") as String?)?.let {
            systemProperty("gtnhextractor.packVersion", it)
        }
        (project.findProperty("extractorSha") as String?)?.let {
            systemProperty("gtnhextractor.extractorSha", it)
        }
        (project.findProperty("modVersions") as String?)?.let {
            systemProperty("gtnhextractor.modVersions", it)
        }
        (project.findProperty("debugMeta") as String?)?.let {
            systemProperty("gtnhextractor.debugMeta", it)
        }
    }
}
