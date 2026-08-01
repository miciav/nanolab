# Plan the Release From the Commit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every input that decides what a release builds derive from the guarded commit, not from the working checkout.

**Architecture:** The release preflight extracts the guarded commit into a caller-owned temporary directory and computes the image matrix, the ARM64 matrix and the publication plan from that tree. The function catalog gains an explicit root parameter (default: today's global) so image planning stops depending on `NANOFAAS_ROOT` agreeing with the root it was handed. The working checkout stays authoritative for git state, credential-location validation, Ansible assets and the `docs/performance` output.

**Tech Stack:** Python 3.12, Typer, Sonata Engine, pytest, `git archive`, `tarfile`.

## Global Constraints

- Design document: `docs/superpowers/specs/2026-08-01-plan-release-from-commit-design.md`.
- `source_tree` is a **required** keyword argument on `build_release_request`. No implicit fallback to the working checkout.
- Both `nanolab run` and `nanolab plan` supply a `source_tree`; `plan` must stay offline.
- Run tests per package: `pytest -c packages/<pkg>/pyproject.toml packages/<pkg>/tests`. A bare `uv run pytest` fails collection on duplicate test basenames.
- Export `NANOFAAS_ROOT` pointing at a **clean** nanoFaaS checkout before running anything below; a checkout carrying leftovers from another branch produces unrelated failures.
- Do not change what `resolve_function_definition` reads at run time (`release/benchmark.py`). It stays on the global.
- Do not share one archive between the preflight and the source resource. Two `git archive` calls of the same commit are equivalent and the resource re-verifies git state.

---

### Task 1: Give the function catalog an explicit root

**Files:**
- Modify: `packages/nanolab/src/nanolab/functions/catalog.py:202-241`
- Test: `packages/nanolab/tests/test_function_catalog.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `list_functions(root: Path | None = None) -> list[FunctionDefinition]` and `resolve_function_definition(key: str, root: Path | None = None) -> FunctionDefinition`. `None` keeps reading `default_tool_paths().nanofaas_root`. Task 2 calls `list_functions(repo_root)`.

- [ ] **Step 1: Write the failing tests**

Append to `packages/nanolab/tests/test_function_catalog.py`:

```python
def test_catalog_discovers_from_an_explicit_root(tmp_path: Path) -> None:
    """An explicit root makes discovery independent of NANOFAAS_ROOT."""
    example = tmp_path / "functions" / "python" / "solo"
    example.mkdir(parents=True)
    (example / "function.yaml").write_text(
        "name: solo\nruntime: python\nfamily: solo\n", encoding="utf-8"
    )

    keys = {function.key for function in list_functions(tmp_path)}

    assert "python-solo" in keys


def test_catalog_roots_do_not_leak_into_each_other(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root, family in ((first, "alpha"), (second, "beta")):
        example = root / "functions" / "python" / family
        example.mkdir(parents=True)
        (example / "function.yaml").write_text(
            f"name: {family}\nruntime: python\nfamily: {family}\n", encoding="utf-8"
        )

    first_keys = {function.key for function in list_functions(first)}
    second_keys = {function.key for function in list_functions(second)}

    assert "python-alpha" in first_keys and "python-alpha" not in second_keys
    assert "python-beta" in second_keys and "python-beta" not in first_keys


def test_catalog_default_root_is_the_configured_checkout(nanofaas_root: Path) -> None:
    assert [function.key for function in list_functions()] == [
        function.key for function in list_functions(nanofaas_root)
    ]
```

Add `from pathlib import Path` and `list_functions` to the module's imports if absent.

- [ ] **Step 2: Run the tests and verify they fail**

Run:
```bash
NANOFAAS_ROOT=$NANOFAAS_ROOT uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml \
  packages/nanolab/tests/test_function_catalog.py -q -p no:randomly --no-cov
```
Expected: FAIL with `TypeError: list_functions() takes 0 positional arguments but 1 was given`.

- [ ] **Step 3: Thread the root through the catalog**

In `packages/nanolab/src/nanolab/functions/catalog.py`, replace the four entry
points. Note that `scenario_payloads_dir` belongs to the **tool** root and must
keep coming from `default_tool_paths()` — only the nanoFaaS root is overridable:

```python
def _load_functions(root: Path | None = None) -> tuple[FunctionDefinition, ...]:
    paths = default_tool_paths()
    functions = (
        *_discover_example_functions(
            (Path(root) if root is not None else paths.nanofaas_root) / "functions",
            paths.scenario_payloads_dir,
        ),
        *_FIXTURE_FUNCTIONS,
    )
    seen: set[str] = set()
    for function in functions:
        if function.key in seen:
            raise ValueError(f"Duplicate function key: {function.key}")
        seen.add(function.key)
    return functions


def _function_index(root: Path | None = None) -> dict[str, FunctionDefinition]:
    return {function.key: function for function in _load_functions(root)}


def list_functions(root: Path | None = None) -> list[FunctionDefinition]:
    return list(_load_functions(root))


def resolve_function_definition(
    key: str, root: Path | None = None
) -> FunctionDefinition:
    return _definition_from_index(_function_index(root), key)
```

- [ ] **Step 4: Run the tests and verify they pass**

Run the command from Step 2. Expected: PASS.

- [ ] **Step 5: Run the whole nanolab suite for regressions**

```bash
NANOFAAS_ROOT=$NANOFAAS_ROOT uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q -p no:randomly
```
Expected: 821 passed — the 818 that passed before this plan, plus the 3 new tests.

- [ ] **Step 6: Commit**

```bash
git add packages/nanolab/src/nanolab/functions/catalog.py packages/nanolab/tests/test_function_catalog.py
git commit -m "feat: let the function catalog discover from an explicit root"
```

---

### Task 2: Plan images from the root the caller handed in

**Files:**
- Modify: `packages/nanolab/src/nanolab/images/plan.py:125-162`
- Test: `packages/nanolab/tests/images/test_plan.py`

**Interfaces:**
- Consumes: `list_functions(root)` from Task 1.
- Produces: `build_image_plan(repo_root, ...)` now discovers functions under `repo_root` instead of under `NANOFAAS_ROOT`. Signature unchanged. Task 3 relies on this to plan from the extraction.

- [ ] **Step 1: Write the failing test**

Append to `packages/nanolab/tests/images/test_plan.py`:

```python
def test_plan_discovers_functions_under_the_given_root(tmp_path: Path) -> None:
    """The matrix follows the root it is handed, not NANOFAAS_ROOT.

    Before this, _all_targets read the global catalog while _function_target
    computed example_dir.relative_to(repo_root), so a divergence raised an
    opaque ValueError instead of planning the given tree.
    """
    for relative in (
        "platform/control-plane",
        "services/java/warm-echo",
        "runtimes/watchdog",
        "functions/python/solo",
    ):
        target = tmp_path / relative
        target.mkdir(parents=True)
        (target / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (tmp_path / "functions/python/solo/function.yaml").write_text(
        "name: solo\nruntime: python\nfamily: solo\n", encoding="utf-8"
    )

    plan = build_image_plan(tmp_path, "v0.0.1", registry=REGISTRY)

    assert "python-solo" in plan.target_names
    assert not any(name.startswith("java-") for name in plan.target_names)
```

- [ ] **Step 2: Run the test and verify it fails**

Run:
```bash
NANOFAAS_ROOT=$NANOFAAS_ROOT uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml \
  "packages/nanolab/tests/images/test_plan.py::test_plan_discovers_functions_under_the_given_root" \
  -q -p no:randomly --no-cov
```
Expected: FAIL — either `ValueError` from `relative_to` or a `FileNotFoundError` naming a Dockerfile from the real checkout, because discovery used the global root.

- [ ] **Step 3: Pass the root to the catalog**

In `packages/nanolab/src/nanolab/images/plan.py`, inside `_all_targets`, change
the single call:

```python
        *(
            _function_target(repo_root, function)
            for function in list_functions(repo_root)
            if function.example_dir is not None
        ),
```

- [ ] **Step 4: Run the test and verify it passes**

Run the command from Step 2. Expected: PASS.

- [ ] **Step 5: Run the image and release suites**

```bash
NANOFAAS_ROOT=$NANOFAAS_ROOT uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml \
  packages/nanolab/tests/images packages/nanolab/tests/release packages/nanolab/tests/plans \
  -q -p no:randomly --no-cov
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add packages/nanolab/src/nanolab/images/plan.py packages/nanolab/tests/images/test_plan.py
git commit -m "fix: plan images from the root the caller handed in"
```

---

### Task 3: Extract the commit and plan the release from it

**Files:**
- Modify: `packages/nanolab/src/nanolab/release/build.py` (add `extract_commit_tree` beside `create_source_archive`)
- Modify: `packages/nanolab/src/nanolab/plans/release.py:100-235` (`ReleaseRequest`, `build_release_request`) and `:447,539` (`build_arm64_image_plan`, `build_publish_plan` calls)
- Test: `packages/nanolab/tests/release/test_build.py`
- Test: `packages/nanolab/tests/plans/test_release.py`

**Interfaces:**
- Consumes: `build_image_plan(repo_root, ...)` from Task 2.
- Produces:
  - `extract_commit_tree(repo_root: Path, commit: str, destination: Path) -> Path` — extracts the commit into `destination` (created if absent) and returns it.
  - `ReleaseRequest.source_tree: Path` — the extracted tree.
  - `build_release_request(..., source_tree: Path)` — required keyword argument. Task 4 supplies it from a `TemporaryDirectory`.

- [ ] **Step 1: Write the failing extraction test**

Append to `packages/nanolab/tests/release/test_build.py`:

```python
def test_extract_commit_tree_ignores_worktree_only_paths(tmp_path: Path) -> None:
    """The extraction is the commit, so ignored and untracked junk cannot leak."""
    from nanolab.release.build import extract_commit_tree

    repo = tmp_path / "repo"
    (repo / "functions/python/solo").mkdir(parents=True)
    (repo / "functions/python/solo/function.yaml").write_text("name: solo\n", encoding="utf-8")
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    for argv in (
        ("git", "init", "-q"),
        ("git", "add", "-A"),
        ("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"),
    ):
        subprocess.run(argv, cwd=repo, check=True)
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    # Leftovers of the shape that broke the release: gitignored, so the tree stays clean.
    (repo / "functions/java/figlet/build").mkdir(parents=True)
    (repo / "functions/java/figlet/payloads").mkdir(parents=True)

    destination = extract_commit_tree(repo, commit, tmp_path / "tree")

    assert (destination / "functions/python/solo/function.yaml").is_file()
    assert not (destination / "functions/java/figlet").exists()
```

- [ ] **Step 2: Run the test and verify it fails**

Run:
```bash
NANOFAAS_ROOT=$NANOFAAS_ROOT uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml \
  "packages/nanolab/tests/release/test_build.py::test_extract_commit_tree_ignores_worktree_only_paths" \
  -q -p no:randomly --no-cov
```
Expected: FAIL with `ImportError: cannot import name 'extract_commit_tree'`.

- [ ] **Step 3: Implement the extraction**

Add to `packages/nanolab/src/nanolab/release/build.py`, next to
`create_source_archive`, and add `import tarfile` to the module imports:

```python
def extract_commit_tree(repo_root: Path, commit: str, destination: Path) -> Path:
    """Materialize one commit as a plain tree, free of worktree state.

    Planning reads this instead of the checkout, so ignored build output and
    untracked files cannot add phantom targets to the image matrix.
    """
    output = Path(destination)
    output.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".commit-tree.", suffix=".tar")
    os.close(descriptor)
    archive = Path(temporary_name)
    try:
        subprocess.run(
            ("git", "archive", "--format=tar", f"--output={archive}", commit),
            cwd=Path(repo_root),
            check=True,
        )
        with tarfile.open(archive) as bundle:
            bundle.extractall(output, filter="data")
    except (subprocess.CalledProcessError, tarfile.TarError, OSError) as error:
        # The CLI turns ValueError from the preflight into a clean BadParameter;
        # git and tarfile raise neither, so normalize here rather than leaking a
        # traceback out of an offline preflight.
        raise ValueError(f"could not extract release source for {commit}") from error
    finally:
        archive.unlink(missing_ok=True)
    return output
```

- [ ] **Step 4: Run the test and verify it passes**

Run the command from Step 2. Expected: PASS.

- [ ] **Step 5: Write the failing preflight test**

Append to `packages/nanolab/tests/plans/test_release.py`:

```python
def test_build_release_request_plans_from_the_commit_not_the_worktree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    canonical_release_configs: tuple[Path, Path],
) -> None:
    """Gitignored leftovers in the checkout must not reach the image matrix."""
    scenario_path, environment_path = canonical_release_configs
    monkeypatch.setattr(
        release_plan, "git_state", lambda _root: GitState(commit="a" * 40, clean=True)
    )
    extracted: list[tuple[Path, str, Path]] = []

    def fake_extract(repo_root: Path, commit: str, destination: Path) -> Path:
        extracted.append((repo_root, commit, destination))
        return NANOFAAS_ROOT

    monkeypatch.setattr(release_plan, "extract_commit_tree", fake_extract)
    source_tree = tmp_path / "tree"

    request = release_plan.build_release_request(
        repo_root=NANOLAB_ROOT,
        nanofaas_root=NANOFAAS_ROOT,
        scenario_path=scenario_path,
        environment_path=environment_path,
        release_config_path=None,
        run_dir=tmp_path / "run",
        performance_root=tmp_path / "performance",
        source_tree=source_tree,
    )

    assert extracted == [(NANOFAAS_ROOT, "a" * 40, source_tree)]
    assert request.source_tree == NANOFAAS_ROOT
    assert request.image_plan.cells
```

- [ ] **Step 6: Run the test and verify it fails**

Run:
```bash
NANOFAAS_ROOT=$NANOFAAS_ROOT uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml \
  "packages/nanolab/tests/plans/test_release.py::test_build_release_request_plans_from_the_commit_not_the_worktree" \
  -q -p no:randomly --no-cov
```
Expected: FAIL with `TypeError: build_release_request() got an unexpected keyword argument 'source_tree'`.

- [ ] **Step 7: Wire the extraction into the preflight**

In `packages/nanolab/src/nanolab/plans/release.py`:

1. Import the helper next to the other `release_build` uses:

```python
from nanolab.release.build import extract_commit_tree
```

2. Add the field to `ReleaseRequest`, after `performance_root` and before
   `credentials` (it takes no default, so it must precede the defaulted fields).
   A default here would silently plan from the current directory, which is the
   implicit fallback this design exists to remove:

```python
    source_tree: Path
```

   Two tests construct `ReleaseRequest(...)` directly and will need
   `source_tree=Path("/tmp")`: `test_release_request_rejects_non_azure_environment`
   and `test_release_request_is_frozen` in `tests/plans/test_release.py`.

3. Add the required keyword argument to `build_release_request`, after
   `performance_root: Path,`:

```python
    source_tree: Path,
```

4. Immediately after `source_commit = _release_source_commit(source_root, plain_version)`,
   extract and keep the tree:

```python
    # Plan from the commit, never from the checkout: ignored build output and
    # untracked files must not be able to add cells the archive cannot build.
    planning_root = extract_commit_tree(source_root, source_commit, Path(source_tree))
```

5. Change the matrix to read the extraction:

```python
    image_plan = build_image_plan(
        planning_root,
        version_tag,
        registry=DEFAULT_REGISTRY,
        architectures=("amd64",),
    )
```

6. Pass it into the returned request, next to `performance_root=...`:

```python
        source_tree=planning_root,
```

- [ ] **Step 8: Run the test and verify it passes**

Run the command from Step 6. Expected: PASS.

- [ ] **Step 9: Make the workflow builder plan from the same tree**

In `build_release_workflow`, replace the two remaining uses of the checkout for
planning. At the ARM64 phase (`arm_plan = build_arm64_image_plan(`):

```python
    arm_plan = build_arm64_image_plan(
        request.source_tree,
        request.version,
        registry=request.image_plan.registry,
    )
```

and at the publication plan (`pub_plan = release_publish.build_publish_plan(`):

```python
    pub_plan = release_publish.build_publish_plan(
        request.source_tree,
        request.version,
        local_registry=request.image_plan.registry,
    )
```

Leave every other use of `nanofaas` in that function alone: bootstrap, role
bindings, source archiving and `performance_root` belong to the checkout.

- [ ] **Step 10: Run the release and plan suites**

```bash
NANOFAAS_ROOT=$NANOFAAS_ROOT uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml \
  packages/nanolab/tests/release packages/nanolab/tests/plans -q -p no:randomly --no-cov
```
Expected: all pass. Existing `build_release_request` callers in
`tests/plans/test_release.py` now need `source_tree=tmp_path / "tree"`; add it to
each call site the run reports as failing.

- [ ] **Step 11: Commit**

```bash
git add packages/nanolab/src/nanolab/release/build.py packages/nanolab/src/nanolab/plans/release.py packages/nanolab/tests
git commit -m "feat: plan the release from the guarded commit"
```

---

### Task 4: Let the CLI own the extracted tree

**Files:**
- Modify: `packages/nanolab/src/nanolab/cli/product.py:128-155` (`_release_request`), `:339-383` (`run_command`), and the release branch of `plan_command`
- Test: `packages/nanolab/tests/cli/test_release_command.py`

**Interfaces:**
- Consumes: `build_release_request(..., source_tree=...)` from Task 3.
- Produces: `_release_request(scenario_path, environment_path, release_config, run_dir, *, executable: bool, source_tree: Path) -> tuple[ReleaseRequest, object]`.

- [ ] **Step 1: Write the failing test**

Append to `packages/nanolab/tests/cli/test_release_command.py`:

```python
def test_generic_release_run_removes_the_extracted_tree(release_cli_harness) -> None:
    """The extraction is throwaway: nothing survives the command."""
    trees: list[Path] = []
    original = release_plan.build_release_request

    def record(**kwargs):
        trees.append(Path(kwargs["source_tree"]))
        return original(**kwargs)

    release_cli_harness.monkeypatch.setattr(product_module, "build_release_request", record)

    result = release_cli_harness.invoke("--provision")

    assert result.exit_code == 0, result.output
    assert len(trees) == 1
    assert trees[0].is_absolute()
    assert not trees[0].exists()
```

Add `monkeypatch=monkeypatch` to the `SimpleNamespace(...)` built by the
`release_cli_harness` fixture so the test can reach it.

- [ ] **Step 2: Run the test and verify it fails**

Run:
```bash
NANOFAAS_ROOT=$NANOFAAS_ROOT uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml \
  "packages/nanolab/tests/cli/test_release_command.py::test_generic_release_run_removes_the_extracted_tree" \
  -q -p no:randomly --no-cov
```
Expected: FAIL with `KeyError: 'source_tree'`.

- [ ] **Step 3: Take the tree as a parameter in `_release_request`**

In `packages/nanolab/src/nanolab/cli/product.py`:

```python
def _release_request(
    scenario_path: Path,
    environment_path: Path | None,
    release_config: Path | None,
    run_dir: Path | None,
    *,
    executable: bool,
    source_tree: Path,
) -> tuple[ReleaseRequest, object]:
```

and pass it through to `build_release_request`, next to `executable=executable`:

```python
            source_tree=source_tree,
```

- [ ] **Step 4: Own the lifetime in `run_command`**

Add `from contextlib import ExitStack, nullcontext` to the imports (the module
already imports `nullcontext`) and `import tempfile`.

Replace the release preflight block so the temporary tree is created before the
request and released when the command ends:

```python
        release_request: ReleaseRequest | None = None
        release_provider: object | None = None
        release_journal = None
        # The extracted tree is throwaway and only has to outlive workflow
        # compilation; the ExitStack closes it on every exit path.
        lifetime = ExitStack()
        if release:
            source_tree = Path(
                lifetime.enter_context(tempfile.TemporaryDirectory(prefix="nanofaas-release-"))
            )
            release_request, release_provider = _release_request(
                scenario, environment, release_config, run_dir,
                executable=True,
                source_tree=source_tree,
            )
```

Then add a `finally` clause to the existing `try` that wraps the workflow run —
the one whose `except ReleaseRunInProgressError` and `except BaseException`
clauses already exist:

```python
        finally:
            lifetime.close()
```

- [ ] **Step 5: Do the same in `plan_command`**

In the release branch of `plan_command`, replace the `_release_request` call:

```python
        if scenario_config.workflow == "release":
            with tempfile.TemporaryDirectory(prefix="nanofaas-plan-") as source_tree:
                request, provider = _release_request(
                    scenario, environment, release_config, run_dir,
                    executable=False,
                    source_tree=Path(source_tree),
                )
                sonata_workflow = build_release_workflow(request, provider=provider)
                compiled = sonata_workflow.compile(
                    select=Selection(only=only, start=start, until=until)
                )
            _render_compiled(compiled)
            return
```

Leave the existing non-release path below untouched.

- [ ] **Step 6: Run the CLI suite and verify it passes**

```bash
NANOFAAS_ROOT=$NANOFAAS_ROOT uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/cli -q -p no:randomly --no-cov
```
Expected: all pass.

- [ ] **Step 7: Run the full verification**

```bash
export NANOFAAS_ROOT=$NANOFAAS_ROOT
for p in nanolab workflow-tasks sonata-tasks tui-toolkit; do
  uv run --locked --all-packages --all-groups pytest -c packages/$p/pyproject.toml packages/$p/tests -q
done
uv run ruff check packages
uv run basedpyright --project packages/nanolab
uv run basedpyright --project packages/workflow-tasks
for p in nanolab workflow-tasks sonata-tasks tui-toolkit; do
  uv run --locked --all-packages --all-groups lint-imports --config packages/$p/.importlinter --no-cache
done
```
Expected: zero failures, zero errors, all import contracts kept.

- [ ] **Step 8: Prove the offline plan still compiles the real DAG**

```bash
NANOFAAS_ROOT=$NANOFAAS_ROOT uv run --locked --package nanolab \
  nanolab plan packages/nanolab/scenarios-v2/release.yaml \
  --environment packages/nanolab/environments/azure-release.yaml
```
Expected: 39 numbered units, ending in `finalize-release-documentation`, with no
Azure call.

- [ ] **Step 9: Commit**

```bash
git add packages/nanolab/src/nanolab/cli/product.py packages/nanolab/tests/cli/test_release_command.py
git commit -m "feat: own the extracted release tree in the CLI"
```

---

## Verification of the original defect

After Task 4, reproduce the reported failure and confirm it no longer occurs:

1. In a nanoFaaS checkout on a branch that has `functions/java/figlet`, switch to a
   branch that does not (`git switch main`). The gitignored `functions/java/figlet/build/`
   and `payloads/` survive and `git status` still reports a clean tree.
2. Run `nanolab plan packages/nanolab/scenarios-v2/release.yaml --environment <azure-release.yaml>`.
3. Before this plan: `FileNotFoundError: missing image Dockerfile: functions/java/figlet/Dockerfile`.
   After: the full 39-unit DAG, with no `java-figlet` cell.
