# Publish/Attest Empty Data — Fix Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make publish/attest phases handle empty digest data gracefully instead of failing.

**Architecture:** Each publish/attest composite checks whether its input data is empty. If empty, returns an empty `Steps` with a loggable title. The workflow completes cleanly through all 13 phases; publish/attest are no-ops until evidence plumbing is added.

**Tech Stack:** Python 3.12, Sonata Engine

## Global Constraints

- No changes to `ReleaseJournal`, `release/run.py`, `release/publish.py`
- Composite functions remain in `sonata_tasks/release_composites.py`
- Tests follow `RecordingExecutor` pattern

---

### Task 1: publish_architectures_composite handles empty data

**Files:**
- Modify: `packages/sonata-tasks/src/sonata_tasks/release_composites.py` (function `publish_architectures_composite`)

- [ ] **Step 1: Write the test**

In `packages/sonata-tasks/tests/test_release_composites.py`, add:

```python
def test_publish_architectures_skips_when_no_source_digests(self) -> None:
    executor = RecordingExecutor()
    plan = _FakePublishPlan(copies=(
        _FakeCopy(source="reg/ctrl:v1-amd64", destination="ghcr.io/ctrl:v1-amd64"),
    ))
    composite = publish_architectures_composite(plan, executor, "host", {}, "/auth.json")
    workflow = Workflow("publish-arch")
    workflow.add(composite)
    workflow.run()

    # No commands should be executed — empty digests means skip
    assert len(executor.seen) == 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest packages/sonata-tasks/tests/test_release_composites.py::TestPublishArchitecturesComposite::test_publish_architectures_skips_when_no_source_digests -v
```
Expected: FAIL (KeyError or similar)

- [ ] **Step 3: Add empty-data guard**

In `publish_architectures_composite`, add at the top after docstring:

```python
    if not source_digests:
        return Steps(
            title=title,
            steps=(
                CommandTask(
                    title="No architecture images to publish",
                    argv=("true",),
                    executor=executor,
                    role=role,
                ),
            ),
        )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest packages/sonata-tasks/tests/test_release_composites.py::TestPublishArchitecturesComposite::test_publish_architectures_skips_when_no_source_digests -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add packages/sonata-tasks/
git commit -m "fix: publish_architectures skips gracefully with empty digests

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Same for publish_manifests_composite

**Files:**
- Modify: `packages/sonata-tasks/src/sonata_tasks/release_composites.py` (function `publish_manifests_composite`)

- [ ] **Step 1: Write the test**

```python
def test_publish_manifests_skips_when_no_arch_digests(self) -> None:
    executor = RecordingExecutor()
    plan = _FakePublishPlan(manifests=(
        _FakeManifest(reference="ghcr.io/ctrl:v1", sources=("ghcr.io/ctrl:v1-amd64", "ghcr.io/ctrl:v1-arm64")),
    ))
    composite = publish_manifests_composite(plan, executor, "host", {}, "/docker")
    workflow = Workflow("publish-manifest")
    workflow.add(composite)
    workflow.run()

    assert len(executor.seen) == 0
```

- [ ] **Step 2-5:** Same pattern as Task 1. Guard: `if not architecture_digests:` → skip.

---

### Task 3: Same for publish_aliases_composite

**Files:**
- Modify: `packages/sonata-tasks/src/sonata_tasks/release_composites.py` (function `publish_aliases_composite`)

Already handled — the current code checks `if not items.aliases:` and returns a `"true"` step. This already skips gracefully when the plan has no aliases. No change needed for the empty-digests case since `manifest_digests={}` with an empty aliases list is already handled.

But if aliases exist and digests are empty, the `_pin_with_digest` call will raise `KeyError`. Let's add a guard:

- [ ] **Step 1: Write the test**

```python
def test_publish_aliases_skips_when_no_manifest_digests(self) -> None:
    executor = RecordingExecutor()
    plan = _FakePublishPlan(aliases=(
        _FakeAlias(reference="ghcr.io/ctrl:latest", source="ghcr.io/ctrl:v1"),
    ))
    composite = publish_aliases_composite(plan, executor, "host", {}, "/docker")
    workflow = Workflow("publish-alias")
    workflow.add(composite)
    workflow.run()

    assert len(executor.seen) == 0
```

- [ ] **Step 2-5:** Guard: `if items.aliases and not manifest_digests:` → skip.

---

### Task 4: Same for attest_composite

**Files:**
- Modify: `packages/sonata-tasks/src/sonata_tasks/release_composites.py` (function `attest_composite`)

- [ ] **Step 1: Write the test**

```python
def test_attest_skips_when_no_images(self) -> None:
    executor = RecordingExecutor()
    composite = attest_composite(
        images=(),
        predicate_remote=Path("/tmp/predicate.json"),
        sbom_dir_remote=Path("/tmp/sboms"),
        cosign_key="/secrets/key",
        docker_config="/docker",
        executor=executor,
        role="stack",
    )
    workflow = Workflow("attest")
    workflow.add(composite)
    workflow.run()

    assert len(executor.seen) == 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest packages/sonata-tasks/tests/test_release_composites.py::TestAttestComposite::test_attest_skips_when_no_images -v
```
Expected: FAIL (ValueError from Steps with empty steps)

- [ ] **Step 3: Add empty-images guard**

```python
    if not images:
        return Steps(
            title=title,
            steps=(
                CommandTask(
                    title="No images to attest",
                    argv=("true",),
                    executor=executor,
                    role=role,
                ),
            ),
        )
```

- [ ] **Step 4-5:** Test → pass → commit.

---

### Task 5: Remove TODO markers from release.py

**Files:**
- Modify: `packages/nanolab/src/nanolab/plans/release.py`

- [ ] **Step 1: Remove TODO comment**

Remove the `# TODO: plumb digest evidence...` comment added in the previous fix round.

- [ ] **Step 2: Run full test suite**

```bash
uv run ruff check packages/
uv run --locked basedpyright --project packages/nanolab
uv run --locked basedpyright --project packages/sonata-tasks
uv run pytest packages/sonata-tasks/tests/test_release_composites.py packages/nanolab/tests/plans/test_release.py -v
```

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "feat: publish/attest composites skip gracefully with empty data

All four publish/attest composites now handle empty input data:
- publish_architectures: skips when source_digests is empty
- publish_manifests: skips when architecture_digests is empty
- publish_aliases: skips when manifest_digests is empty
- attest: skips when images is empty

Co-Authored-By: Claude <noreply@anthropic.com>"
```
