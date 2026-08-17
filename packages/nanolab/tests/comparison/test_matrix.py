from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanolab.comparison.matrix import build_matrix, write_manifest
from nanolab.images.control_plane_variants import resolve_variants

VARIANTS = resolve_variants(("jvm", "native-os", "native-o3", "native-o3-g1"))


def test_variants_interleave_instead_of_running_in_blocks() -> None:
    """Blocks would make "which variant" and "when it ran" the same axis.

    Any drift over the hour the matrix takes — a noisy neighbour, a filling page
    cache — would then land entirely on whichever builds ran late and be reported
    as a property of those builds.
    """
    order = [cell.label for cell in build_matrix(VARIANTS, 3)]

    assert order[:4] == [
        "jvm run 1",
        "native-os run 1",
        "native-o3 run 1",
        "native-o3-g1 run 1",
    ]
    assert order[4] == "jvm run 2"
    assert len(order) == 12


def test_every_variant_gets_the_same_number_of_runs() -> None:
    cells = build_matrix(VARIANTS, 3)
    per_variant = {v.key: 0 for v in VARIANTS}
    for cell in cells:
        per_variant[cell.variant.key] += 1

    assert set(per_variant.values()) == {3}


def test_each_cell_has_its_own_directory() -> None:
    """Two cells sharing a directory would overwrite one another's summary."""
    root = Path("/runs")
    dirs = [cell.run_dir(root) for cell in build_matrix(VARIANTS, 3)]

    assert len(set(dirs)) == 12
    assert dirs[0] == root / "jvm" / "run-1"


def test_matrix_rejects_an_empty_request() -> None:
    with pytest.raises(ValueError, match="at least one repetition"):
        build_matrix(VARIANTS, 0)
    with pytest.raises(ValueError, match="at least one variant"):
        build_matrix((), 3)


def test_manifest_records_what_was_attempted(tmp_path: Path) -> None:
    """Written up front, so a matrix that dies halfway still says what it was doing."""
    cells = build_matrix(VARIANTS, 3)

    path = write_manifest(
        tmp_path, cells, functions=("word-stats-java", "word-stats-javascript"), registry="r:5000"
    )
    manifest = json.loads(path.read_text(encoding="utf-8"))

    assert manifest["repetitions"] == 3
    assert manifest["functions"] == ["word-stats-java", "word-stats-javascript"]
    assert [v["key"] for v in manifest["variants"]] == [
        "jvm",
        "native-os",
        "native-o3",
        "native-o3-g1",
    ]
    assert manifest["variants"][3]["build_env"]["NATIVE_GC"] == "G1"
    assert manifest["variants"][0]["image"] == "r:5000/nanofaas/control-plane:jvm"
    # The order is the record of how drift was spread; without it a reader cannot
    # tell an interleaved matrix from a blocked one after the fact.
    assert manifest["order"][:2] == ["jvm run 1", "native-os run 1"]
