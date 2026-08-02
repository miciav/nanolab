# Sonata Release Migration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace `nanolab release run` with one robust, resumable Sonata workflow executed through `nanolab run scenarios-v2/release.yaml`.

**Architecture:** Sonata owns the release DAG, resource lifetime, cleanup, journal, resume, and selection. Existing release helpers are extracted from the procedural runner and reused by coarse-grained evidence-producing Sonata tasks; after parity and an Azure canary, the procedural runner and its private journal are deleted.

**Tech Stack:** Python 3.12, Typer, Pydantic, Sonata Engine 0.4, Azure VM provider, Docker Buildx, k3s, k6, GHCR, Cosign, pytest.

---

## Non-negotiable acceptance criteria

- `nanolab plan ...release.yaml` performs no Azure/network operation.
- Fresh execution requires `--provision`; resume requires `--resume` and a journal.
- The committed source SHA and canonical release/environment configuration identify the run.
- Three dedicated VMs are Sonata infrastructure resources; failure destroys them unless `--keep`.
- `--keep` never retains source archives, tunnels, builders, credentials, tokens, or signing keys.
- Benchmarks use digest-pinned release images, never rebuilt `:e2e` images.
- A failed load test, autoscaling check, regression gate, ARM smoke, digest check, publish, or attest stops all later phases.
- Publication uses verified immutable digests; aliases are last.
- Resume reuses only evidence that can still be verified and fails closed otherwise.
- The final code has one run path and one journal implementation.

### Task 1: Lock the unsafe path and encode the target CLI contract

**Files:**
- Modify: `packages/nanolab/src/nanolab/cli/product.py`
- Modify: `packages/nanolab/src/nanolab/cli/release.py`
- Test: `packages/nanolab/tests/cli/test_command_surface.py`
- Test: `packages/nanolab/tests/cli/test_release_command.py`

**Step 1: Write failing CLI tests**

Add tests proving:

```python
def test_generic_release_run_fails_closed_until_sonata_release_is_enabled(...):
    result = runner.invoke(app, ["run", str(RELEASE_SCENARIO), "--environment", str(AZURE)])
    assert result.exit_code != 0
    assert "Sonata release migration is incomplete" in result.output


def test_generic_release_surface_accepts_resume(...):
    result = runner.invoke(app, ["run", str(RELEASE_SCENARIO), "--help"])
    assert "--resume" in result.output
```

Keep `nanolab release run` untouched in this task; it remains the reference until
cutover.

**Step 2: Run the tests and verify RED**

```bash
NANOFAAS_ROOT=/path/to/nanofaas uv run pytest \
  packages/nanolab/tests/cli/test_command_surface.py \
  packages/nanolab/tests/cli/test_release_command.py -q
```

Expected: the new tests fail because generic release execution is still allowed
and `--resume` is absent.

**Step 3: Add the temporary fail-closed guard and `--resume` option**

Add one explicit release guard in `run_command`; keep `plan` available. Add the
option but reject it for non-release workflows until Task 8 wires the journal.

**Step 4: Run the tests and verify GREEN**

**Step 5: Commit**

```bash
git add packages/nanolab/src/nanolab/cli/product.py packages/nanolab/tests/cli
git commit -m "fix: fail closed while Sonata release is incomplete"
```

### Task 2: Make scenario configuration and preflight canonical

**Files:**
- Modify: `packages/nanolab/scenarios-v2/release.yaml`
- Modify: `packages/nanolab/src/nanolab/config/scenario.py`
- Modify: `packages/nanolab/src/nanolab/plans/release.py`
- Reuse: `packages/nanolab/src/nanolab/release/environment.py`
- Reuse: `packages/nanolab/src/nanolab/release/secrets.py`
- Test: `packages/nanolab/tests/plans/test_release.py`
- Test: `packages/nanolab/tests/release/test_environment.py`

**Step 1: Write failing preflight tests**

Cover exact Azure profile validation, non-placeholder operator CIDR, pinned image
URNs, clean committed source, version consistency, credential existence/mode,
credentials outside both repositories, and a dynamic non-empty image matrix.
Remove any assertion that the matrix must contain exactly 26 cells.

Also assert the canonical profile equals existing performance history:

```yaml
profile: azure-d8s-v5+d2s-v5-amd64-native-loadtest-v1
benchmark_scenario: loadtest.yaml
throughput_max_loss_percent: 10
p95_max_increase_percent: 15
error_rate_max: 0.30
```

**Step 2: Verify RED**

Run `packages/nanolab/tests/plans/test_release.py` and the environment tests.

**Step 3: Implement one `build_release_request()` preflight**

It must call the existing `validate_release_environment()` and
`CredentialFiles.validate()`, expand user paths once at the configuration
boundary, compute source/config/environment digests, and build the live image
matrix. It must not construct a cloud client or resolve an IP.

**Step 4: Verify GREEN and commit**

```bash
git commit -am "refactor: centralize release preflight"
```

### Task 3: Extract tested domain helpers from the procedural runner

**Files:**
- Create: `packages/nanolab/src/nanolab/release/build.py`
- Create: `packages/nanolab/src/nanolab/release/benchmark.py`
- Modify: `packages/nanolab/src/nanolab/release/run.py`
- Move tests from: `packages/nanolab/tests/release/test_run_amd64.py`
- Create: `packages/nanolab/tests/release/test_build.py`
- Create: `packages/nanolab/tests/release/test_benchmark.py`

**Step 1: Move tests before code**

Move, without weakening, tests for source archive creation/staging, generated
BuildKit/Bake inputs, AMD64/ARM64 commands, image digest inspection, local registry
push, ARM smoke, pinned benchmark images, aggregation and regression evaluation.

**Step 2: Verify the moved tests fail to import**

**Step 3: Move the corresponding helpers unchanged**

Move pure/build helpers from `release/run.py:277-648,1398-2026` into the two new
modules. Keep provider retry and result checking shared in the smallest existing
module rather than copying them. `run.py` temporarily imports these helpers so the
legacy tests remain green.

**Step 4: Run old and new release suites**

```bash
NANOFAAS_ROOT=/path/to/nanofaas uv run pytest packages/nanolab/tests/release -q
```

**Step 5: Commit**

```bash
git add packages/nanolab/src/nanolab/release packages/nanolab/tests/release
git commit -m "refactor: extract release phase helpers"
```

### Task 4: Put VM provisioning and security inside Sonata resources

**Files:**
- Create: `packages/nanolab/src/nanolab/release/resources.py`
- Modify: `packages/nanolab/src/nanolab/plans/release.py`
- Reuse: `packages/sonata-tasks/src/sonata_tasks/vm.py`
- Reuse: `packages/nanolab/src/nanolab/release/environment.py`
- Test: `packages/nanolab/tests/release/test_resources.py`
- Test: `packages/nanolab/tests/plans/test_release.py`

**Step 1: Write fake-provider lifecycle tests**

Assert acquire order `stack -> loadgen -> arm-builder`, bootstrap per role,
post-ensure Azure fact verification, bounded endpoint rules, ARM-only registry
rule, reverse cleanup, acquire compensation, and `--keep` retention of only VMs.

**Step 2: Verify RED**

**Step 3: Implement release VM resources**

Compose the existing generic `vm_resource()` with release bootstrap/security. Do
not call the legacy `provision_environment()` from the generic release path. Make
endpoint resolution lazy from acquired VM state; compilation must require no IP.

**Step 4: Add an offline-plan test**

Use a provider whose every method raises. `build_release_workflow(...).compile()`
must still succeed.

**Step 5: Verify GREEN and commit**

```bash
git commit -am "feat: manage release infrastructure as Sonata resources"
```

### Task 5: Add release evidence tasks and the Sonata journal boundary

**Files:**
- Create: `packages/nanolab/src/nanolab/release/tasks.py`
- Create: `packages/nanolab/src/nanolab/release/evidence.py`
- Modify: `packages/nanolab/src/nanolab/plans/release.py`
- Test: `packages/nanolab/tests/release/test_tasks.py`
- Test: `packages/nanolab/tests/release/test_evidence.py`

**Step 1: Write failing evidence tests**

Test `file-digest`, local-registry digest and authenticated GHCR digest verifiers.
Unknown kinds, missing files, unreachable registries, invalid output and changed
digests must return false. Test reuse keys change for every release identity or
phase input change and contain no secret content.

**Step 2: Verify RED**

**Step 3: Implement one reusable phase base class**

Create a small `ReleasePhaseTask(ReusableTask)` base that returns only evidence,
uses a deterministic secret-free `reuse_key`, and leaves each phase's actual work
to a callable. Do not add a release-specific abstraction to `sonata_tasks`.

**Step 4: Implement source-test, AMD64-build and registry-push phase tasks**

Each phase writes a compact receipt under the versioned run directory. Registry
push evidence must cover the complete current AMD64 matrix by digest.

**Step 5: Wire `JournalConfig` and verifier injection in a test harness**

Run once, resume once, mutate one evidence item, resume again; assert only the
unsafe suffix reruns.

**Step 6: Verify GREEN and commit**

### Task 6: Benchmark the exact release artifacts and enforce the gate

**Files:**
- Modify: `packages/nanolab/src/nanolab/release/tasks.py`
- Modify: `packages/nanolab/src/nanolab/release/benchmark.py`
- Modify: `packages/nanolab/src/nanolab/plans/release.py`
- Test: `packages/nanolab/tests/release/test_tasks.py`
- Test: `packages/nanolab/tests/plans/test_release.py`

**Step 1: Write failing benchmark contract tests**

Assert every `build_loadtest_plan()` call receives digest-pinned
`prebuilt_control_plane_image` and `prebuilt_function_images`, and has
`build_images=False`. Assert each run has an isolated directory and stale files
cannot satisfy a missing result.

**Step 2: Write the failing publication-barrier test**

Return `RegressionDecision(passed=False, ...)`; assert the task raises and no ARM
or publish task runs.

**Step 3: Implement benchmark, aggregate and gate phase tasks**

Reuse the extracted helpers. The gate writes `regression-decision.json`, returns
its digest as evidence, and raises with all failure reasons when `passed` is false.
Parse existing camelCase performance records through the canonical parser already
used by the legacy implementation.

**Step 4: Verify RED-to-GREEN and commit**

```bash
git commit -am "feat: gate release images with Sonata benchmarks"
```

### Task 7: Make ARM64 staging, build and smoke evidence-complete

**Files:**
- Modify: `packages/nanolab/src/nanolab/release/resources.py`
- Modify: `packages/nanolab/src/nanolab/release/tasks.py`
- Modify: `packages/nanolab/src/nanolab/plans/release.py`
- Modify or delete: `packages/sonata-tasks/src/sonata_tasks/release_composites.py`
- Test: `packages/nanolab/tests/release/test_tasks.py`
- Test: `packages/nanolab/tests/plans/test_release.py`

**Step 1: Write failing ARM contract tests**

Assert the identical source archive is checksum-verified on both stack and ARM,
ARM Bake/BuildKit files are transferred, one builder is created, the tunnel is
idempotently acquired, every ARM image is pushed, and registry digests cover the
complete ARM matrix before smoke passes.

**Step 2: Add failure tests**

Inject failure at transfer, builder bootstrap, individual build, push, digest
verification and smoke. Assert tunnel/builder/source cleanup always occurs and
publish never starts.

**Step 3: Implement resources and phase tasks using extracted helpers**

Remove the duplicate builder creation currently split between resource and
composite. Prefer the tested domain helpers; delete unused composite code rather
than retaining two implementations.

**Step 4: Verify GREEN and commit**

### Task 8: Stage credentials, publish by digest, attest and finalize

**Files:**
- Modify: `packages/nanolab/src/nanolab/release/resources.py`
- Modify: `packages/nanolab/src/nanolab/release/tasks.py`
- Reuse: `packages/nanolab/src/nanolab/release/secrets.py`
- Reuse: `packages/nanolab/src/nanolab/release/publish.py`
- Reuse: `packages/nanolab/src/nanolab/release/attest.py`
- Modify: `packages/nanolab/src/nanolab/plans/release.py`
- Test: `packages/nanolab/tests/release/test_tasks.py`
- Test: `packages/nanolab/tests/release/test_secrets.py`

**Step 1: Write credential-resource tests**

Assert validation precedes any cloud action, GHCR and Cosign files are transferred
only around their consumers, remote mode is restrictive, values never enter argv,
events, metadata, journals or exceptions, and cleanup runs on success/failure/
interrupt and with `--keep`.

**Step 2: Write publish ordering and evidence tests**

Require passing gate and ARM smoke receipts. Assert immutable architecture copies
use source digests, manifests reference verified architecture digests, aliases
reference verified manifests, attestations target published digests, verification
precedes finalize, and a documentation failure leaves finalize reusable.

**Step 3: Implement credential resources and four terminal phase tasks**

Wrap the existing staging context managers as non-infrastructure Sonata resources.
Reuse `publish.py` and `attest.py`; do not reimplement registry or Cosign commands.

**Step 4: Run secret-leak and failure-injection suites, then commit**

### Task 9: Wire generic CLI execution, lock, resume and metadata

**Files:**
- Modify: `packages/nanolab/src/nanolab/cli/product.py`
- Modify: `packages/nanolab/src/nanolab/plans/release.py`
- Modify: `packages/nanolab/src/nanolab/cli/provisioning.py`
- Test: `packages/nanolab/tests/cli/test_command_surface.py`
- Test: `packages/nanolab/tests/cli/test_release_command.py`

**Step 1: Write end-to-end fake-provider CLI tests**

Cover fresh `--provision`, rejected fresh run without acknowledgement, `--resume`,
resume without journal, `--keep`, Ctrl-C cleanup, concurrent-run rejection,
versioned default run directory, failure metadata, and a passed full execution.

**Step 2: Verify RED**

**Step 3: Remove the temporary guard and wire execution**

The release path must bypass outer `provision_environment()`, because VM resources
are in the Sonata DAG. Pass `JournalConfig`, `resume`, release verifiers and
selection directly to `Workflow.run()`. Use a versioned run directory, never
`runs/release/latest`, and acquire the release-wide lock before the first cloud
resource.

**Step 4: Verify GREEN and commit**

### Task 10: Prove parity, canary on Azure, then delete legacy execution

**Files:**
- Modify: `packages/nanolab/src/nanolab/cli/release.py`
- Delete: procedural orchestration remaining in `packages/nanolab/src/nanolab/release/run.py`
- Delete: `packages/nanolab/src/nanolab/release/state.py` after Sonata journal parity
- Delete or migrate: `packages/nanolab/tests/release/test_run_amd64.py`
- Delete: `packages/nanolab/tests/release/test_state.py` after equivalent Sonata tests exist
- Delete: `packages/nanolab/release.yaml`
- Modify: `packages/nanolab/README.md`
- Modify: `.gitignore` only if obsolete paths remain

**Step 1: Run all local verification**

```bash
NANOFAAS_ROOT=/path/to/nanofaas uv run pytest -q
uv run ruff check packages
uv run basedpyright --project packages/nanolab
uv run lint-imports --config packages/nanolab/pyproject.toml
```

Expected: zero failures; no hard-coded image-cell count.

**Step 2: Run an Azure canary through the regression gate**

Use a real bounded operator CIDR and a new patch-version candidate:

```bash
caffeinate -i ./nanolab.sh run packages/nanolab/scenarios-v2/release.yaml \
  --environment packages/nanolab/environments/azure.yaml \
  --release-config packages/nanolab/scenarios-v2/release-config.yaml \
  --provision --until evaluate-regression-gate --keep
```

Verify VM facts, NSG sources, exact pinned images, journal evidence and cleanup of
all non-infrastructure resources. Then explicitly tear down the retained canary.

**Step 3: Run one full non-production-namespace rehearsal**

Use a registry namespace where tags may be deleted. Inject one interruption and
prove `--resume` reuses the verified prefix.

**Step 4: Perform the real full release and verify remote evidence**

Confirm GHCR digests/manifests/aliases, Cosign attestations, performance record,
history update, journal, metadata, and zero remaining Azure resources.

**Step 5: Remove legacy command and code**

Keep only `nanolab release prepare`. Remove `release plan` and `release run`, the
procedural orchestrator, private release journal, obsolete configuration and tests.
Update README examples to generic `plan`/`run` plus `--resume`.

Also move the VM providers out of `workflow_tasks/vm/` into `sonata-tasks`, beside the
`vm_resource` that already wraps them. They are shared today — the import contract
`packages/sonata-tasks/.importlinter` forbids only `workflow_tasks.core`, `.workflows`
and `.workflow`, and `sonata_tasks/vm.py` builds on `workflow_tasks.vm.adapters` — so
the move waits for this step: deleting the procedural runner is what leaves the provider
with a single consumer. Moving it earlier would mean editing both paths while the
release path is still unproven.

**Step 6: Prove deletion did not reduce coverage**

Repeat the complete local suite and assert:

```bash
rg -n "run_amd64_release|ReleaseJournal|nanolab release run" packages docs/plans README.md
```

Expected: no production references; historical plans may be excluded from the
search or explicitly marked superseded.

**Step 7: Commit the cutover**

```bash
git add -A
git commit -m "refactor: make Sonata the only release runner"
```

## Implementation order rule

Do not start the Azure canary, remove `nanolab release run`, or weaken the
fail-closed guard until Tasks 1-9 are green. Do not fix the current Sonata workflow
with isolated command/path patches: each migrated phase must carry cleanup,
evidence, resume and publication-barrier tests in the same task.

