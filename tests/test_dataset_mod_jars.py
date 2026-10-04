"""The pinned mod jars (``dataset/mod_jars.py``): their URLs are the ones the spike checked.

``docs/spikes/329-me-ae2.md`` section 9.1 fetched each ME jar from these exact URLs (HTTP 200, sha1
matching Nexus). A pin is three fields and everything else derives from them, so these hold the
derivation to those literal strings: a typo in a group, artifact or version would otherwise surface
only as a preview that silently draws no AE2 art.
"""

from __future__ import annotations

import pytest

from gtnh_solver.dataset.mod_jars import (
    AE2,
    AE2FC,
    ME_JARS,
    ME_PACK_VERSION,
    JarSpec,
    gt5u_jar,
)


def test_the_me_jar_urls_are_the_ones_the_spike_checked() -> None:
    assert AE2.url == (
        "https://nexus.gtnewhorizons.com/repository/public/com/github/GTNewHorizons/"
        "Applied-Energistics-2-Unofficial/rv3-beta-1050-GTNH/"
        "Applied-Energistics-2-Unofficial-rv3-beta-1050-GTNH.jar"
    )
    assert AE2FC.url == (
        "https://nexus.gtnewhorizons.com/repository/public/com/github/GTNewHorizons/"
        "AE2FluidCraft-Rework/1.5.106-gtnh/AE2FluidCraft-Rework-1.5.106-gtnh.jar"
    )


def test_each_pin_names_its_namespace_its_source_and_its_pack() -> None:
    assert (AE2.modid, AE2FC.modid) == ("appliedenergistics2", "ae2fc")
    assert AE2.source_repo == "https://github.com/GTNewHorizons/Applied-Energistics-2-Unofficial"
    assert AE2FC.source_repo == "https://github.com/GTNewHorizons/AE2FluidCraft-Rework"
    assert ME_JARS == (AE2, AE2FC)
    assert ME_PACK_VERSION == "2.9.0-beta-3"


def test_gt5u_jar_names_the_artifact_the_manifest_was_read_at() -> None:
    gt = gt5u_jar("5.09.54.133")
    assert gt.modid == "gregtech"
    assert gt.jar_name == "GT5-Unofficial-5.09.54.133.jar"
    assert gt.maven_path == (
        "com/github/GTNewHorizons/GT5-Unofficial/5.09.54.133/GT5-Unofficial-5.09.54.133.jar"
    )


def test_a_group_outside_github_has_no_source_repo_to_name() -> None:
    spec = JarSpec(modid="x", artifact="y", version="1", group="org.example")
    assert spec.url.endswith("/org/example/y/1/y-1.jar")
    with pytest.raises(ValueError, match=r"org\.example"):
        _ = spec.source_repo
