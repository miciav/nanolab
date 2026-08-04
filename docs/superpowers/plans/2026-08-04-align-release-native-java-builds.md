# Align the Release Workflow with nanoFaaS Unified Java Native Builds

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build every Java native release image through nanoFaaS's shared `deploy/native-java/Dockerfile` instead of the retired Paketo `bootBuildImage` task, so `nanolab run scenarios-v2/release.yaml` completes against current nanoFaaS main.

**Architecture:** nanoFaaS commit `c3179fbb` ("Unify Java native builds", 2026-08-01) replaced per-project Spring buildpacks with one parameterized Dockerfile driven by three build args, and made `bootBuildImage` throw a `GradleException` so the old path cannot be used by accident. nanolab currently models those six cells as a separate `build_kind="gradle"` partition that shells out to `./gradlew`. This plan folds them into the existing Buildx Bake partition — the same graph the release already digest-pins — by giving `ImageCell` a flavor-dependent Dockerfile, context and build-arg set. The `gradle` partition then has no members and is deleted.

**Tech Stack:** Python 3.12, Pydantic, Typer, Docker Buildx Bake, GraalVM CE 25.2.4, Gradle 9.3.1, pytest, Sonata Engine.

## Global Constraints

- `NANOFAAS_ROOT` must point at a **clean** nanoFaaS git tree for the whole suite; `build_release_request` calls `git_state` for real and a dirty tree fails with "release requires a clean nanoFaaS Git tree" before any assertion runs.
- Run pytest **per package**: `pytest -c packages/<pkg>/pyproject.toml packages/<pkg>/tests`. A bare `uv run pytest` fails collection because test basenames repeat across packages that have no `__init__.py`.
- Never assert a raw image-matrix cell count. Assert the expansion rule (flavors per runtime from `list_functions()`, per-architecture symmetry). The catalog grows and counts rot silently.
- The three build args and their values must stay byte-identical to `scripts/native-java-image.sh` in the pinned nanoFaaS tree: `NATIVE_TASK`, `NATIVE_BINARY`, `GRADLE_ARGS`.
- The shared native Dockerfile lives at `deploy/native-java/Dockerfile` and requires the **repository root** as build context (it does `COPY . .`).
- Reference nanoFaaS state for this work: `~/Downloads/mcFaas` at `edd69b1e` (main, v0.18.2 prepared). CI pin `f18f1be6` already contains `deploy/native-java/Dockerfile`, so CI stays green without a re-pin.

---

### Task 1: Model the native build contract as bake cells

**Files:**
- Modify: `packages/nanolab/src/nanolab/images/plan.py`
- Test: `packages/nanolab/tests/images/test_plan.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `NATIVE_JAVA_DOCKERFILE: Path` — module constant, `Path("deploy/native-java/Dockerfile")`.
  - `NativeBuild` frozen dataclass with fields `task: str`, `binary: Path`, `gradle_args: tuple[str, ...] = ()`.
  - `ImageTarget.native_build: NativeBuild | None = None` replacing `native_gradle_task`, `native_image_property`, `native_extra_arguments`.
  - `ImageCell.dockerfile -> Path`, `ImageCell.context -> Path`, `ImageCell.build_args -> dict[str, str]`.

- [ ] **Step 1: Write the failing tests**

Add to `packages/nanolab/tests/images/test_plan.py`. That file already defines a module-level `NANOFAAS_ROOT` constant and a `_plan(**kwargs)` helper that pins version `v0.18.0` and the test registry — use them, and extend the existing `from nanolab.images.plan import build_image_plan` line rather than adding a second import:

```python
from nanolab.images.plan import NATIVE_JAVA_DOCKERFILE, build_image_plan


def test_java_native_cells_build_from_the_shared_native_dockerfile() -> None:
    plan = _plan(architectures=("amd64",))
    native = [
        cell
        for cell in plan.cells
        if cell.flavor == "native" and cell.target.native_build is not None
    ]
    assert native, "expected Java native cells in the matrix"
    for cell in native:
        assert cell.dockerfile == NATIVE_JAVA_DOCKERFILE
        assert cell.context == Path(".")
        assert set(cell.build_args) == {"NATIVE_TASK", "NATIVE_BINARY", "GRADLE_ARGS"}


def test_control_plane_native_cell_carries_the_script_build_args() -> None:
    plan = _plan(architectures=("amd64",))
    cell = next(
        cell
        for cell in plan.cells
        if cell.target.name == "control-plane" and cell.flavor == "native"
    )
    assert cell.build_args == {
        "NATIVE_TASK": ":control-plane:nativeCompile",
        "NATIVE_BINARY": "platform/control-plane/build/native/nativeCompile/control-plane",
        "GRADLE_ARGS": "-PcontrolPlaneModules=all",
    }


def test_java_function_native_cells_derive_task_and_binary_from_the_family() -> None:
    plan = _plan(architectures=("amd64",))
    checked = 0
    for cell in plan.cells:
        native = cell.target.native_build
        if cell.flavor != "native" or native is None:
            continue
        if not cell.target.name.startswith("java-") or cell.target.name == "java-warm-echo":
            continue
        family = cell.target.name.removeprefix("java-")
        assert native.task == f":functions:java:{family}:nativeCompile"
        assert native.binary == Path(
            f"functions/java/{family}/build/native/nativeCompile/{family}"
        )
        checked += 1
    assert checked, "expected Java function native cells in the matrix"


def test_no_cell_invokes_the_retired_buildpack_task() -> None:
    for cell in _plan(architectures=("amd64",)).cells:
        native = cell.target.native_build
        assert native is None or "bootBuildImage" not in native.task


def test_jvm_and_default_cells_keep_their_own_dockerfile_and_context() -> None:
    for cell in _plan(architectures=("amd64",)).cells:
        if cell.flavor == "native" and cell.target.native_build is not None:
            continue
        assert cell.dockerfile == cell.target.dockerfile
        assert cell.context == cell.target.context
        assert cell.build_args == {}
```

`Path` and `pytest` are already imported at the top of that file.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
export NANOFAAS_ROOT=$HOME/Downloads/mcFaas
uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml \
  packages/nanolab/tests/images/test_plan.py -q
```

Expected: FAIL with `ImportError: cannot import name 'NATIVE_JAVA_DOCKERFILE'`.

- [ ] **Step 3: Add the model**

In `packages/nanolab/src/nanolab/images/plan.py`, after the `DEFAULT_REGISTRY` constant:

```python
NATIVE_JAVA_DOCKERFILE = Path("deploy/native-java/Dockerfile")


@dataclass(frozen=True)
class NativeBuild:
    """How nanoFaaS builds one Java native image since commit `c3179fbb`.

    These are the three build args `scripts/native-java-image.sh` feeds to the
    shared Dockerfile. nanolab bakes them rather than shelling out to the
    script, so native cells stay inside the single buildx graph the release
    already digest-pins and verifies.
    """

    task: str
    binary: Path
    gradle_args: tuple[str, ...] = ()
```

Replace the three native fields on `ImageTarget`:

```python
@dataclass(frozen=True)
class ImageTarget:
    name: str
    flavors: tuple[ImageFlavor, ...]
    dockerfile: Path
    context: Path
    native_build: NativeBuild | None = None
    jvm_prerequisite_arguments: tuple[str, ...] = ()
```

- [ ] **Step 4: Give `ImageCell` a flavor-dependent build surface**

In `ImageCell`, replace the `gradle_command` property with these three, and leave `platform` and `prerequisite_command` untouched for now:

```python
    @property
    def native_build(self) -> NativeBuild | None:
        """The native contract for this cell, or None if it is not a Java native cell."""
        if self.flavor != "native":
            return None
        return self.target.native_build

    @property
    def dockerfile(self) -> Path:
        native = self.native_build
        return NATIVE_JAVA_DOCKERFILE if native is not None else self.target.dockerfile

    @property
    def context(self) -> Path:
        # The shared native Dockerfile does `COPY . .` — it only builds from the
        # repository root, never from the target's own directory.
        native = self.native_build
        return Path(".") if native is not None else self.target.context

    @property
    def build_args(self) -> dict[str, str]:
        native = self.native_build
        if native is None:
            return {}
        return {
            "NATIVE_TASK": native.task,
            "NATIVE_BINARY": native.binary.as_posix(),
            "GRADLE_ARGS": " ".join(native.gradle_args),
        }
```

`GRADLE_ARGS` is emitted even when empty, matching the script, which always passes the arg.

- [ ] **Step 5: Point the targets at the new contract**

In `_all_targets`, replace the control-plane and warm-echo native metadata:

```python
        ImageTarget(
            name="control-plane",
            flavors=("jvm", "native"),
            dockerfile=Path("platform/control-plane/Dockerfile"),
            context=Path("platform/control-plane"),
            native_build=NativeBuild(
                task=":control-plane:nativeCompile",
                binary=Path("platform/control-plane/build/native/nativeCompile/control-plane"),
                gradle_args=("-PcontrolPlaneModules=all",),
            ),
            jvm_prerequisite_arguments=(
                ":control-plane:bootJar",
                "-PcontrolPlaneModules=all",
            ),
        ),
        ImageTarget(
            name="java-warm-echo",
            flavors=("jvm", "native"),
            dockerfile=Path("services/java/warm-echo/Dockerfile"),
            context=Path("services/java/warm-echo"),
            native_build=NativeBuild(
                task=":services:java:warm-echo:nativeCompile",
                binary=Path("services/java/warm-echo/build/native/nativeCompile/warm-echo"),
            ),
            jvm_prerequisite_arguments=(":services:java:warm-echo:bootJar",),
        ),
```

In `_function_target`, replace the `function.runtime == "java"` branch's native metadata:

```python
    if function.runtime == "java":
        return ImageTarget(
            name=name,
            flavors=("jvm", "native"),
            dockerfile=source_dir / "Dockerfile",
            context=source_dir,
            native_build=NativeBuild(
                task=f":functions:java:{function.family}:nativeCompile",
                binary=Path(
                    f"functions/java/{function.family}"
                    f"/build/native/nativeCompile/{function.family}"
                ),
            ),
            jvm_prerequisite_arguments=(
                f":functions:java:{function.family}:bootJar",
            ),
        )
```

Leave the `java-lite` branch alone: it has no `native_build`, and its own multi-stage Dockerfile with a root context already works.

- [ ] **Step 6: Make every cell a bake cell**

In `_cell`, the `build_kind` argument becomes constant — no target produces Gradle cells any more:

```python
        build_kind="bake",
    )
```

- [ ] **Step 7: Validate the shared Dockerfile exists**

In `_validate_targets`, replace the `missing` computation so the shared native Dockerfile is checked too. Without this, a nanoFaaS checkout predating `c3179fbb` would plan cells whose Dockerfile is absent and fail only on hardware:

```python
    required = {target.dockerfile for target in targets}
    required.update(
        NATIVE_JAVA_DOCKERFILE for target in targets if target.native_build is not None
    )
    missing = sorted(
        path.as_posix() for path in required if not (repo_root / path).is_file()
    )
    if missing:
        raise FileNotFoundError(f"missing image Dockerfile: {', '.join(missing)}")
```

- [ ] **Step 8: Run the tests to verify they pass**

```bash
export NANOFAAS_ROOT=$HOME/Downloads/mcFaas
uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml \
  packages/nanolab/tests/images/test_plan.py -q
```

Expected: PASS. Other suites may now fail on `gradle_command` — that is Task 3's job, not a reason to stop here.

- [ ] **Step 9: Commit**

```bash
git add packages/nanolab/src/nanolab/images/plan.py packages/nanolab/tests/images/test_plan.py
git commit -m "feat: model Java native images as shared-Dockerfile bake cells"
```

---

### Task 2: Emit build args in the Bake file

**Files:**
- Modify: `packages/nanolab/src/nanolab/images/bake.py:63-75`
- Test: `packages/nanolab/tests/images/test_bake.py`

**Interfaces:**
- Consumes: `ImageCell.dockerfile`, `ImageCell.context`, `ImageCell.build_args` from Task 1.
- Produces: Bake targets carrying an `"args"` key for Java native cells. `render_bake` and `render_bake_json` keep their existing signatures.

Without this task the native cells render with no args, and `deploy/native-java/Dockerfile` compiles nothing — `$NATIVE_TASK` is empty and `./gradlew` builds the default task.

- [ ] **Step 1: Write the failing tests**

Add to `packages/nanolab/tests/images/test_bake.py`. That file already defines a module-level `NANOFAAS_ROOT` and a `_plan()` helper pinning version `v0.18.0` and registry `registry.test:5000/nanofaas` across both architectures — use it:

```python
def test_native_cells_render_with_root_context_and_build_args() -> None:
    rendered = render_bake(_plan())
    target = rendered["target"]["control-plane-amd64-native"]
    assert target["context"] == "."
    assert target["dockerfile"] == "deploy/native-java/Dockerfile"
    assert target["args"] == {
        "NATIVE_TASK": ":control-plane:nativeCompile",
        "NATIVE_BINARY": "platform/control-plane/build/native/nativeCompile/control-plane",
        "GRADLE_ARGS": "-PcontrolPlaneModules=all",
    }
    assert target["platforms"] == ["linux/amd64"]


def test_jvm_cells_render_without_an_args_key() -> None:
    target = render_bake(_plan())["target"]["control-plane-amd64-jvm"]
    assert target["context"] == "platform/control-plane"
    assert target["dockerfile"] == "Dockerfile"
    assert "args" not in target


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
```

`render_bake` and `build_image_plan` are already imported at the top of that file.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
export NANOFAAS_ROOT=$HOME/Downloads/mcFaas
uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml \
  packages/nanolab/tests/images/test_bake.py -q
```

Expected: FAIL with `KeyError: 'args'`.

- [ ] **Step 3: Render from the cell instead of the target**

Replace `_bake_target` in `packages/nanolab/src/nanolab/images/bake.py`:

```python
def _bake_target(cell: ImageCell) -> dict[str, list[str] | str | dict[str, str]]:
    dockerfile = cell.dockerfile
    if cell.context != Path("."):
        dockerfile = dockerfile.relative_to(cell.context)
    target: dict[str, list[str] | str | dict[str, str]] = {
        "context": cell.context.as_posix(),
        "dockerfile": dockerfile.as_posix(),
        "platforms": [cell.platform],
        "tags": [cell.image],
    }
    args = cell.build_args
    if args:
        target["args"] = args
    return target
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
export NANOFAAS_ROOT=$HOME/Downloads/mcFaas
uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml \
  packages/nanolab/tests/images/test_bake.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/nanolab/src/nanolab/images/bake.py packages/nanolab/tests/images/test_bake.py
git commit -m "feat: render native Java build args into the bake file"
```

---

### Task 3: Delete the empty Gradle build partition

**Files:**
- Modify: `packages/nanolab/src/nanolab/images/plan.py`
- Modify: `packages/nanolab/src/nanolab/release/build.py:381-391`
- Modify: `packages/nanolab/src/nanolab/release/arm.py:136-145`
- Modify: `packages/nanolab/src/nanolab/cli/images.py:293-301`
- Test: `packages/nanolab/tests/release/test_build.py`
- Test: `packages/nanolab/tests/release/test_arm.py`
- Test: `packages/nanolab/tests/cli/test_images_command.py`

**Interfaces:**
- Consumes: `build_kind="bake"` for every cell, from Task 1.
- Produces: `ImagePlan.cells` is the only cell collection. `ImagePlan.bake_cells`, `ImagePlan.gradle_cells`, `ImageCell.build_kind`, `ImageCell.gradle_command` and the `BuildKind` alias no longer exist. `amd64_build_commands`, `arm64_build_commands` and `_plan_specs` keep their existing signatures.

After Task 1 the Gradle partition is always empty, so its consumers loop over nothing. This task removes them rather than leaving three dead loops and an arm64 Paketo workaround that can never run.

- [ ] **Step 1: Write the failing tests**

In `packages/nanolab/tests/release/test_build.py` (the module imports `release_build` and builds its plan inline with `NANOFAAS_ROOT`):

```python
def test_amd64_commands_contain_no_gradle_image_builds() -> None:
    plan = build_image_plan(NANOFAAS_ROOT, "v9.9.9", architectures=("amd64",))

    commands = release_build.amd64_build_commands(
        plan,
        builder_name="release-amd64-9.9.9",
        remote_bake_file="/remote/docker-bake.json",
        remote_source_dir="/remote/source",
    )

    assert not any(spec.task_id.startswith("release.images.native.") for spec in commands)
    assert not any("bootBuildImage" in " ".join(spec.argv) for spec in commands)
    assert any(spec.task_id == "release.images.bake.amd64" for spec in commands)
```

In `packages/nanolab/tests/release/test_arm.py` (the module imports `arm` and has a `_plan()` helper wrapping `arm.build_arm64_image_plan`). Note the full keyword set — `arm64_build_commands` also requires `remote_buildkit_config` and `registry_upstream`:

```python
def test_arm64_commands_contain_no_gradle_image_builds() -> None:
    commands = arm.arm64_build_commands(
        _plan(),
        builder_name="nanofaas-release-v0-18-0",
        remote_bake_file="/srv/release/docker-bake-arm64.json",
        remote_buildkit_config="/srv/release/buildkitd.toml",
        remote_source_dir="/srv/source",
        registry_upstream="203.0.113.10",
    )

    assert not any(spec.task_id.startswith("release.arm64.native.") for spec in commands)
    assert not any("dashaun/builder" in " ".join(spec.argv) for spec in commands)
    assert any(spec.task_id == "release.images.bake.arm64" for spec in commands)
```

- [ ] **Step 1b: Rewrite the test that asserts the partition itself**

`test_arm64_plan_partitions_the_live_matrix_without_loss` in `packages/nanolab/tests/release/test_arm.py:25` asserts the split directly and cannot survive this task. Replace its body — keep asserting shape, never a count:

```python
def test_arm64_plan_covers_the_live_matrix_without_loss() -> None:
    """The matrix grows with the function catalog, so assert shape, not a count."""
    plan = _plan()

    assert plan.cells
    assert {cell.architecture for cell in plan.cells} == {"arm64"}
    assert len({(cell.target.name, cell.flavor) for cell in plan.cells}) == len(plan.cells)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
export NANOFAAS_ROOT=$HOME/Downloads/mcFaas
uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml \
  packages/nanolab/tests/release/test_build.py packages/nanolab/tests/release/test_arm.py -q
```

Expected: FAIL — the `release.images.native.*` / `release.arm64.native.*` specs are still emitted (with empty `argv`, since `gradle_command` now returns `None` for every cell).

- [ ] **Step 3: Remove the Gradle surface from the plan model**

In `packages/nanolab/src/nanolab/images/plan.py`:

- Delete the `BuildKind` type alias.
- Delete the `build_kind` field from `ImageCell` and the `build_kind="bake"` argument from `_cell`.
- Delete the `bake_cells` and `gradle_cells` properties from `ImagePlan`. (`gradle_command` is already gone — Task 1 replaced it.)
- Simplify `prerequisite_command`, which no longer needs the partition check:

```python
    @property
    def prerequisite_command(self) -> tuple[str, ...] | None:
        if self.flavor != "jvm":
            return None
        return ("./gradlew", *self.target.jvm_prerequisite_arguments)
```

- [ ] **Step 4: Update the three consumers**

In `packages/nanolab/src/nanolab/release/build.py`, inside `amd64_build_commands`: change `for cell in plan.bake_cells:` to `for cell in plan.cells:` and delete the trailing `commands.extend(...)` block that iterates `plan.gradle_cells`.

In `packages/nanolab/src/nanolab/release/arm.py`, inside `arm64_build_commands`: the same two edits — `for cell in plan.bake_cells:` becomes `for cell in plan.cells:`, and the trailing `commands.extend(...)` over `plan.gradle_cells` goes.

In `packages/nanolab/src/nanolab/cli/images.py`, inside `_plan_specs`: change `for cell in plan.bake_cells:` to `for cell in plan.cells:`, change `if any(cell.architecture == architecture for cell in plan.bake_cells):` to use `plan.cells`, and delete the trailing `specs.extend(...)` over `plan.gradle_cells`.

In `packages/nanolab/src/nanolab/images/bake.py`, `render_bake` iterates `plan.bake_cells` — change it to `plan.cells`.

- [ ] **Step 5: Remove assertions on the deleted partition**

Grep the suite for the names you just deleted and update every hit. Tests asserting the bake/gradle split must now assert the expansion rule instead — flavors per runtime and per-architecture symmetry — never a cell count:

```bash
grep -rn "gradle_cells\|bake_cells\|build_kind\|gradle_command\|native_gradle_task\|native_image_property\|native_extra_arguments" \
  packages/nanolab/src packages/nanolab/tests
```

Expected after the edits: no hits.

- [ ] **Step 6: Run the full nanolab suite**

```bash
export NANOFAAS_ROOT=$HOME/Downloads/mcFaas
uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
```

Expected: PASS. If exactly one release test fails with "release requires a clean nanoFaaS Git tree", the checkout is dirty — clean it and re-run; that failure names the guard, not a regression.

- [ ] **Step 7: Run the static gates**

```bash
uv run --locked --all-packages --all-groups ruff check packages
uv run --locked --all-packages --all-groups basedpyright --project packages/nanolab
uv run --locked --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
```

Expected: all clean.

- [ ] **Step 8: Commit**

```bash
git add packages/nanolab/src packages/nanolab/tests
git commit -m "refactor: one image build partition, no Gradle image cells"
```

---

### Task 4: Prove the compiled release plan on the real checkout

**Files:**
- Modify: `packages/nanolab/README.md:149-156`
- Modify: `.github/workflows/ci.yml:32` (only if Step 3 shows the pin is too old)
- Modify: `README.md` (the pinned-commit note, only alongside a CI re-pin)

**Interfaces:**
- Consumes: the compiled plan from Tasks 1–3.
- Produces: a verified bake file and a green `nanolab plan` for `scenarios-v2/release.yaml`. No new code.

- [ ] **Step 1: Render the bake file and read the native targets**

```bash
export NANOFAAS_ROOT=$HOME/Downloads/mcFaas
uv run --package nanolab python -c "
from pathlib import Path
from nanolab.images.plan import build_image_plan
from nanolab.images.bake import render_bake
plan = build_image_plan(Path.home()/'Downloads/mcFaas', 'v0.18.2', architectures=('amd64',))
rendered = render_bake(plan)
for name, target in sorted(rendered['target'].items()):
    if 'args' in target:
        print(name, target['context'], target['dockerfile'], target['args'])
"
```

Expected: exactly six lines — `control-plane`, `java-warm-echo`, `java-figlet`, `java-json-transform`, `java-roman-numeral`, `java-word-stats` — each with context `.`, dockerfile `deploy/native-java/Dockerfile`, and the task/binary pair for its module. If a name is missing or an extra one appears, stop: the catalog changed and Task 1's target mapping needs the same change.

- [ ] **Step 2: Cross-check the mapping against the nanoFaaS script**

```bash
grep -A3 -E '^\s+(control-plane|warm-echo|word-stats|json-transform|roman-numeral|figlet)\)' \
  $HOME/Downloads/mcFaas/scripts/native-java-image.sh
```

Every `task=` and `binary=` printed here must equal the `NATIVE_TASK` / `NATIVE_BINARY` from Step 1 for the same module. This is the contract; a mismatch is a bug in Task 1, not in nanoFaaS.

- [ ] **Step 3: Confirm the CI pin still satisfies the new validation**

```bash
git -C $HOME/Downloads/mcFaas show \
  "$(grep -oE '[0-9a-f]{40}' .github/workflows/ci.yml | head -1):deploy/native-java/Dockerfile" \
  >/dev/null && echo "pin OK" || echo "pin too old — re-pin required"
```

At the time of writing the pin is `f18f1be6`, which already contains the file, so this prints "pin OK" and no workflow edit is needed. If it prints otherwise, bump the `ref:` in `.github/workflows/ci.yml` and the matching commit noted in the root `README.md`, in one commit.

- [ ] **Step 4: Compile the whole release plan**

```bash
export NANOFAAS_ROOT=$HOME/Downloads/mcFaas
./nanolab.sh plan \
  packages/nanolab/scenarios-v2/release.yaml \
  --environment packages/nanolab/environments/azure-release.yaml \
  --release-config packages/nanolab/scenarios-v2/release-config.yaml \
  --run-dir packages/nanolab/runs/canary
```

Expected: the same 45 nodes as before, exit 0, no Azure call. The node list is a compile-time property and must not shrink — the native builds moved inside `008.build-amd64-images`, they did not disappear.

- [ ] **Step 5: Correct the README's release description**

`packages/nanolab/README.md` describes the matrix as "the 52-cell image matrix". Replace the hard-coded count, which rots as the function catalog grows, and state where native images now come from:

```markdown
## Image releases

`nanolab images` renders and builds the full image matrix anywhere without
publishing. Java native images build through nanoFaaS's shared
`deploy/native-java/Dockerfile`, parameterized by `NATIVE_TASK`,
`NATIVE_BINARY` and `GRADLE_ARGS` — the same contract as
`scripts/native-java-image.sh`. The Spring Boot buildpack path was retired in
nanoFaaS `c3179fbb` and now throws on use.

Official releases run only through `nanolab run scenarios-v2/release.yaml` on
the pinned Azure profile, after `nanolab release prepare` has committed the
version. The standalone release configuration is in
[`release.yaml`](release.yaml). GitHub Actions never publishes images, and
local/Multipass/Proxmox builds cannot promote to GHCR.
```

- [ ] **Step 6: Commit**

```bash
git add packages/nanolab/README.md
git commit -m "docs: describe the unified native Java image build"
```

---

### Task 5: Resume the v0.18.2 release on Azure

**Files:** none — this task executes the workflow and records its outcome.

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces: a completed release, or a named failure at a node past `008`.

The failed run left a journal at `packages/nanolab/runs/canary/releases/0.18.2/sonata.jsonl`. Resuming reuses the verified source tests and provisioning (~10 minutes) and restarts at the AMD64 build.

- [ ] **Step 1: Refresh the operator CIDR**

```bash
curl -s https://ifconfig.me; echo
grep operator_source_cidr packages/nanolab/environments/azure-release.yaml
```

If they differ, update the value and the date in the comment above it, and commit. A stale CIDR does not surface until `capture-prometheus-snapshot` times out mid-benchmark, roughly an hour in.

- [ ] **Step 2: Confirm the nanoFaaS tree is clean**

```bash
git -C $HOME/Downloads/mcFaas status --short
```

Expected: empty output. Anything else fails the release preflight.

- [ ] **Step 3: Resume the release**

```bash
export NANOFAAS_ROOT=$HOME/Downloads/mcFaas
./nanolab.sh run \
  packages/nanolab/scenarios-v2/release.yaml \
  --environment packages/nanolab/environments/azure-release.yaml \
  --release-config packages/nanolab/scenarios-v2/release-config.yaml \
  --resume \
  --run-dir packages/nanolab/runs/canary
```

Do **not** pipe this through `tee` — the pipe masks the exit status and a failed run reports success. Redirect instead: `> release.log 2>&1` and read the file, or watch it directly.

Expected: nodes 001–007 skip on verified evidence, `008.build-amd64-images` runs the six native builds through the shared Dockerfile, and the run proceeds to publish and attest.

- [ ] **Step 4: Verify no infrastructure survived**

```bash
az vm list -g maurinoRicerca-rg --query "[?contains(name,'nanofaas')].name" -o tsv
az disk list -g maurinoRicerca-rg --query "[?contains(name,'nanofaas')].name" -o tsv
```

Expected: empty on both, whether the run passed or failed. Sonata destroys its resources on failure; anything left is a resource-lifetime bug worth reporting.

- [ ] **Step 5: Commit the release output in nanoFaaS**

A completed release writes `docs/performance/history.md` and `docs/performance/releases/v0.18.2.json` into the nanoFaaS checkout. Commit them, or the next test run fails the clean-tree guard:

```bash
git -C $HOME/Downloads/mcFaas add docs/performance
git -C $HOME/Downloads/mcFaas commit -m "chore: record the v0.18.2 release metrics"
```

- [ ] **Step 6: Commit the nanolab run journal**

```bash
git add packages/nanolab/runs/canary packages/nanolab/scenarios-v2/release.yaml \
  packages/nanolab/environments/azure-release.yaml
git commit -m "chore: record the v0.18.2 canary release run"
```

---

## Notes for the implementer

**Why bake instead of calling the script.** `scripts/native-java-image.sh` runs `docker build` directly. Routing native cells through it would put six images outside the buildx graph that `registry_push_composite` and `exact_receipt_artifacts` digest-pin and verify, and the release's whole evidence chain rests on that pinning. Feeding the same three build args into bake keeps one graph, one set of digests, one receipt.

**Why the arm64 Paketo workaround disappears.** `gradle_command` injected `-PimageBuilder=dashaun/builder:tiny` for arm64 because Paketo's default builder had no arm64 image. `deploy/native-java/Dockerfile` resolves GraalVM from `TARGETARCH`, so a native arm64 build on the arm-builder VM needs no special casing. Task 3 deletes the workaround; there is nothing to port.

**What this plan does not touch.** The `java-lite` targets already build from a self-contained multi-stage Dockerfile with a root context and never used `bootBuildImage`. Their cells are unchanged, and no task should modify them.
