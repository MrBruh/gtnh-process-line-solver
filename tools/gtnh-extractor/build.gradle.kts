
plugins {
    id("com.gtnewhorizons.gtnhconvention")
}

// Forward the dataset-run properties the CI (and the boot-verify command) pass on the Gradle
// command line into the forked run JVM as system properties, where DumperMod reads them.
//   -PdatasetOut=<dir>     where to emit <dir>/multiblocks/ (resolved against this project dir)
//   -PtextureOut=<dir>     where to emit <dir>/manifest.json (lane 6, texture pass; texture-only
//                          when -PdatasetOut is absent, so the run skips the structure dump)
//   -PpackVersion=<ver>    the GTNH pack release the dump tracks (recorded in _meta.json)
//   -PextractorSha=<sha>   git SHA of the extractor that produced the dump
//   -PmodVersions=<a=1,b=2> pinned tracked-mod versions from gtnh.lock.json (provenance; the
//                          runtime Forge container is the fallback and GT5U's reports "MC1710")
//   -PinjectIcons=<bool>   override whether the texture pass injects NamedIcons over GT's icon
//                          fields (default: on a server, off on a client - see TextureDumper)
//
// Both run families, not just runServer. The texture pass has a client route (it reads the
// stitched atlas rather than recovering names the SideTransformer deleted), and a `it.name ==
// "runServer"` match forwarded none of these to it - the run booted and then dumped nothing,
// because every property it reads was absent. lwjgl3ify contributes runClient17/21/25 and the
// matching server variants, so match by prefix rather than listing them.
// The RFG run tasks are JavaExec-based; guard the cast so a future task-type change fails clearly.
tasks.matching { it.name.startsWith("runServer") || it.name.startsWith("runClient") }.configureEach {
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
        (project.findProperty("injectIcons") as String?)?.let {
            systemProperty("gtnhextractor.injectIcons", it)
        }
    }
}
