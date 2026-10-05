"""The mod jars the previewer reads textures from, pinned to the pack they ship in.

A texture is never committed here: the previewer fetches the mod's own jar from the GTNH Nexus at
preview time and reads the PNGs it needs straight out of it (:mod:`gtnh_solver.previewer.jar`).
This module says WHICH jars, as data: a :class:`JarSpec` names one Maven artifact and the asset
namespace (``assets/<modid>/``) it serves, and from it follow the jar's file name, its Nexus URL
and the source repository its tag lives in. GT's jar follows the dataset manifest's provenance
(:func:`gt5u_jar`); the ME jars are pinned below.

**The ME pins** are the two mods an ME network is drawn from, at the versions the 2.9.0-beta-3
pack ships (``DreamAssemblerXXL`` ``releases/manifests/2.9.0-beta-3.json``), the same pins
``docs/spikes/329-me-ae2.md`` reads its rules from. Both are group ``com.github.GTNewHorizons``,
and each version is also the git tag of the source it was built from:

- AE2, GT:NH's fork (mod id ``appliedenergistics2``): ``Applied-Energistics-2-Unofficial``
  ``rv3-beta-1050-GTNH``;
- AE2FluidCraft, "FC" (mod id ``ae2fc``): ``AE2FluidCraft-Rework`` ``1.5.106-gtnh``.

Each Nexus URL these produce was checked by the spike (section 9.1: HTTP 200, sha1 matching
Nexus), and their texture trees match the source clones file for file. GT itself compiles against
AE2 ``rv3-beta-1049-GTNH``; the pack ships 1050, which is what runs, so 1050 is what is drawn.

**A pack bump re-checks these.** The pins are not read from ``gtnh.lock.json``, because the
extractor never touches either mod; they move with the pack by hand, as the reference clones in
``../gtnh-reference`` do, and :data:`ME_PACK_VERSION` says which pack they were last checked
against. The derived ME render data (``data/ae2/<AE2 version>/render.json``) is namespaced by
:data:`AE2`'s version for the same reason: a new pin is a new folder, never a silent edit.

**Licences differ, and that is a property of the jar, not of this code.** GT's art is LGPL-3.0
like its code; AE2's code is LGPL-3.0 but its textures and models are CC BY-NC-SA 3.0 (spike 9.2).
FC declares LGPL-3.0 throughout, but whether any of its sprites derive from AE2's art was never
checked, so FC art is treated as AE2's. A preview that embeds either therefore credits AE2 and
links that licence, and ``NOTICE`` says such a preview may be shared only non-commercially.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The GTNH Nexus repository every pinned jar is fetched from.
NEXUS_URL = "https://nexus.gtnewhorizons.com/repository/public"

#: The Maven group GTNH publishes its GitHub-built mods under (JitPack's ``com.github.<owner>``).
GTNH_GROUP = "com.github.GTNewHorizons"

_GITHUB_GROUP_PREFIX = "com.github."


@dataclass(frozen=True, slots=True)
class JarSpec:
    """One mod jar on the Nexus, and the asset namespace a texture request is routed to it by.

    ``modid`` is the ``assets/<modid>/`` folder the jar's textures live under, which is how
    :func:`gtnh_solver.previewer.jar.multi_jar_png_provider` picks the jar for an asset path; the
    other three fields are the Maven coordinates. Everything else derives from those, so a pin is
    one line and its URL cannot drift from its version.
    """

    modid: str
    artifact: str
    version: str
    group: str = GTNH_GROUP

    @property
    def jar_name(self) -> str:
        """The jar's file name, which is also its cache file name (versions cache side by side)."""
        return f"{self.artifact}-{self.version}.jar"

    @property
    def maven_path(self) -> str:
        """The jar's path inside a Maven repository: ``group/as/dirs/artifact/version/jar``."""
        return f"{self.group.replace('.', '/')}/{self.artifact}/{self.version}/{self.jar_name}"

    @property
    def url(self) -> str:
        """The jar's GTNH Nexus URL."""
        return f"{NEXUS_URL}/{self.maven_path}"

    @property
    def source_repo(self) -> str:
        """The GitHub repository the jar is built from; its git tag is :attr:`version`.

        GTNH's group is JitPack's ``com.github.<owner>`` form, so the owner is in the group. A
        group outside that form has no repository to name, which is an error rather than a guess.
        """
        if not self.group.startswith(_GITHUB_GROUP_PREFIX):
            raise ValueError(f"{self.group} is not a com.github.<owner> group")
        owner = self.group.removeprefix(_GITHUB_GROUP_PREFIX)
        return f"https://github.com/{owner}/{self.artifact}"


#: The pack the ME pins below were taken from (spike, "Sources").
ME_PACK_VERSION = "2.9.0-beta-3"

#: GT:NH's Applied Energistics 2 fork: cables, buses, the interface, the controller, drive and
#: energy acceptor (spike 9.1).
AE2 = JarSpec(
    modid="appliedenergistics2",
    artifact="Applied-Energistics-2-Unofficial",
    version="rv3-beta-1050-GTNH",
)

#: AE2FluidCraft-Rework: the fluid import, export and storage buses and the Dual Interface
#: (spike 9.1).
AE2FC = JarSpec(modid="ae2fc", artifact="AE2FluidCraft-Rework", version="1.5.106-gtnh")

#: Every jar an ME network's art comes from, in the order a preview asks them.
ME_JARS: tuple[JarSpec, ...] = (AE2, AE2FC)

#: GT5-Unofficial's artifact, and the asset namespace its own textures live under. The jar also
#: carries its addon packages' namespaces (``miscutils``, ``bartworks``, ...), which is why the
#: previewer routes every namespace no other jar claims to it.
GT5U_ARTIFACT = "GT5-Unofficial"
GT5U_MODID = "gregtech"


def gt5u_jar(version: str) -> JarSpec:
    """The GT5-Unofficial jar at ``version`` (the dataset manifest says which one it was read at)."""
    return JarSpec(modid=GT5U_MODID, artifact=GT5U_ARTIFACT, version=version)
