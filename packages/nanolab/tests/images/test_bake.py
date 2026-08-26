from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from nanolab.images.bake import render_bake, render_bake_json
from nanolab.images.plan import build_image_plan


NANOFAAS_ROOT = Path(os.environ["NANOFAAS_ROOT"]).resolve()


def _plan():  # noqa: ANN202
    return build_image_plan(
        NANOFAAS_ROOT,
        "v0.18.0",
        registry="registry.test:5000/nanofaas",
    )


def test_bake_json_is_deterministic_and_has_expected_groups() -> None:
    plan = _plan()

    first = render_bake_json(plan)
    second = render_bake_json(plan)
    document = json.loads(first)

    assert first == second
    assert list(document) == ["group", "target"]
    assert list(document["group"]) == [
        "default",
        "docker-amd64",
        "docker-arm64",
        "docker-all",
    ]
    bake_per_arch = len(plan.cells) // 2
    assert len(document["group"]["docker-amd64"]["targets"]) == bake_per_arch
    assert len(document["group"]["docker-arm64"]["targets"]) == bake_per_arch
    assert document["group"]["docker-all"]["targets"] == (
        document["group"]["docker-amd64"]["targets"]
        + document["group"]["docker-arm64"]["targets"]
    )
    assert document["group"]["default"] == document["group"]["docker-all"]


def test_bake_targets_have_unique_names_tags_and_single_platforms() -> None:
    plan = _plan()
    document = render_bake(plan)
    targets = document["target"]
    tags = [target["tags"][0] for target in targets.values()]

    assert len(targets) == len(plan.cells)
    assert len(targets) == len(set(targets))
    assert len(tags) == len(set(tags))
    assert all(len(target["tags"]) == 1 for target in targets.values())
    assert all(len(target["platforms"]) == 1 for target in targets.values())
    assert all("context" in target and "dockerfile" in target for target in targets.values())


def test_bake_rendering_uses_repo_relative_context_and_dockerfile() -> None:
    document = render_bake(_plan())

    assert document["target"]["java-word-stats-amd64-jvm"] == {
        "context": "functions/java/word-stats",
        "dockerfile": "Dockerfile",
        "platforms": ["linux/amd64"],
        "args": {"JVM_TUNING": "-XX:+UseSerialGC"},
        "tags": [
            "registry.test:5000/nanofaas/java-word-stats:v0.18.0-amd64-jvm"
        ],
    }
    assert document["target"]["go-word-stats-arm64-default"]["context"] == "."
    assert (
        document["target"]["go-word-stats-arm64-default"]["dockerfile"]
        == "functions/go/word-stats/Dockerfile"
    )


def test_bake_selector_filters_targets_and_groups() -> None:
    document = render_bake(_plan(), selectors=("watchdog",))

    assert list(document["target"]) == [
        "watchdog-amd64-default",
        "watchdog-arm64-default",
    ]
    assert document["group"] == {
        "default": {
            "targets": ["watchdog-amd64-default", "watchdog-arm64-default"]
        },
        "docker-amd64": {"targets": ["watchdog-amd64-default"]},
        "docker-arm64": {"targets": ["watchdog-arm64-default"]},
        "docker-all": {
            "targets": ["watchdog-amd64-default", "watchdog-arm64-default"]
        },
    }


def test_bake_selector_rejects_flavor_filter_with_no_matching_cells() -> None:
    """Java targets always have Bake cells now, so the guard needs a target that
    is real but whose flavors never include the requested one: watchdog only
    ever builds the "default" flavor, so filtering it to "native" selects
    nothing, and the guard in `render_bake` must still catch that.
    """
    with pytest.raises(ValueError, match="selector has no Bake cells: watchdog"):
        render_bake(_plan(), selectors=("watchdog",), flavors=("native",))


def test_generated_json_roundtrips_through_buildx_print(tmp_path: Path) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Buildx is absent")
    version = subprocess.run(
        [docker, "buildx", "version"],
        cwd=NANOFAAS_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if version.returncode != 0:
        detail = f"{version.stdout}\n{version.stderr}".lower()
        if "not a docker command" in detail or "unknown command" in detail:
            pytest.skip("Docker Buildx is absent")
        pytest.fail(f"Docker Buildx version check failed:\n{detail}")

    plan = _plan()
    bake_file = tmp_path / "docker-bake.json"
    bake_file.write_text(render_bake_json(plan), encoding="utf-8")
    rendered = subprocess.run(
        [docker, "buildx", "bake", "--file", str(bake_file), "--print"],
        cwd=NANOFAAS_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert rendered.returncode == 0, rendered.stderr
    assert len(json.loads(rendered.stdout)["target"]) == len(plan.cells)


def test_native_cells_render_with_root_context_and_build_args() -> None:
    rendered = render_bake(_plan())
    target = rendered["target"]["control-plane-amd64-native"]
    assert target["context"] == "."
    assert target["dockerfile"] == "deploy/native-java/Dockerfile"
    assert target["args"] == {
        "NATIVE_TASK": ":control-plane:nativeCompile",
        "NATIVE_BINARY": "platform/control-plane/build/native/nativeCompile/control-plane",
        "GRADLE_ARGS": (
            "-PcontrolPlaneModules=all -PnativeOptimization=3 -PnativeGc=G1"
        ),
        "GRAALVM_DISTRIBUTION": "oracle",
    }
    assert target["platforms"] == ["linux/amd64"]


def test_jvm_cells_render_with_the_jvm_c2_tuning() -> None:
    target = render_bake(_plan())["target"]["control-plane-amd64-jvm"]
    assert target["args"] == {"JVM_TUNING": "-XX:+UseSerialGC"}


def test_every_native_java_cell_reaches_the_bake_groups() -> None:
    plan = _plan()
    rendered = render_bake(plan)
    expected = {
        f"{cell.target.name}-{cell.architecture}-native"
        for cell in plan.cells
        if cell.flavor == "native" and cell.target.native_build is not None
    }
    assert expected, "expected Java native cells in the matrix"
    grouped = set(rendered["group"]["docker-all"]["targets"])
    assert expected <= grouped
