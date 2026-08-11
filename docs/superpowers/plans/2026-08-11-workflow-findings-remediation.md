# Workflow Findings Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the duplicated facts, brittle assertions and oversized units found in the CI workflows and the Sonata plan builders, so that a value lives in one place and a failure names its own cause.

**Architecture:** Two independent parts. Part A restructures the GitHub Actions workflows around a local composite action and parallel jobs, so setup is written once and every check reports on its own. Part B removes duplication and type confusion from the Sonata plan builders by giving the shared helpers a public home, naming the deployment constants, and splitting the one 703-line function into phase builders. The parts share no interfaces and can be executed in either order, by different people.

**Tech Stack:** GitHub Actions (composite actions, matrix strategy), Python 3.12, uv workspaces, pytest, basedpyright, ruff, Sonata engine.

## Global Constraints

- Python version pinned to `3.12` everywhere (`uv python install 3.12`).
- Java version pinned to `25`, and it must equal `javaVersion` in the pinned nanoFaaS `gradle.properties`.
- Third-party GitHub Actions are pinned by commit SHA with a `# vX.Y.Z` comment.
- Every workflow job declares `timeout-minutes`.
- `nanolab` is a **public** repository; `nanofaas` is **private**. Secrets are unavailable to pull requests from forks.
- All three packages are at version `0.1.0`; no step may hardcode that version.
- Existing behaviour must not change: these are refactors, and every task ends green on `uv run --locked --all-packages --all-groups pytest`, `ruff check packages`, and `basedpyright --project packages/<pkg>` for all three packages.
- Commit after every task. Never bundle two tasks in one commit.

---

# Part A — GitHub Actions workflows

### Task A1: Stop hardcoding wheel versions and anchor the registry assertion

Two expectations duplicate facts that live elsewhere: the wheel filenames repeat the package version, and the "no registry step" gate matches a bare substring anywhere in the plan. Both are the shape that already cost five pushes today.

**Files:**
- Modify: `.github/workflows/ci.yml:125-131` (wheel smoke), `.github/workflows/ci.yml:169-171` (registry assertion)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing. Later tasks move these steps into jobs but keep the commands verbatim.

- [ ] **Step 1: Reproduce the current wheel step locally and confirm it depends on the version**

```bash
cd /Users/micheleciavotta/Downloads/nanolab
grep -n "0.1.0" .github/workflows/ci.yml
grep -h "^version" packages/*/pyproject.toml
```

Expected: three `dist/<pkg>-0.1.0-py3-none-any.whl` in the workflow, three `version = "0.1.0"` in the packages. The workflow restates a fact the packages own.

- [ ] **Step 2: Replace the three filenames with a glob**

In `.github/workflows/ci.yml`, in the `Smoke-test installed wheels` step:

```yaml
        run: |
          uv venv .wheel-smoke
          # Glob, not names: the filenames carry the package version, and
          # repeating it here means a version bump breaks CI for no reason.
          uv pip install dist/*.whl --python .wheel-smoke/bin/python
          .wheel-smoke/bin/python -c "import nanolab, sonata_tasks, tui_toolkit"
          .wheel-smoke/bin/nanolab --help
```

- [ ] **Step 3: Verify the glob installs the same three wheels**

```bash
cd /Users/micheleciavotta/Downloads/nanolab
S=$(mktemp -d)
uv build --all-packages --out-dir "$S/dist"
uv venv "$S/smoke" -q
uv pip install "$S"/dist/*.whl --python "$S/smoke/bin/python" -q
"$S/smoke/bin/python" -c "import nanolab, sonata_tasks, tui_toolkit; print('ok')"
"$S/smoke/bin/nanolab" --help >/dev/null && echo "cli ok"
rm -rf "$S"
```

Expected: `ok` and `cli ok`.

- [ ] **Step 4: Anchor the registry assertion to a task name**

The current line matches `registry` anywhere in the file. Replace it so it asserts the absence of a *task* whose slug ends in a registry step:

```yaml
          # The gate is that no registry bootstrap task exists, not that the
          # word never appears: a substring match over the whole plan fails on
          # any future task that merely mentions a registry.
          if grep -qE '^[0-9]+\.[a-z0-9-]*(acquire|ensure|configure)-[a-z0-9-]*registry' "$provisioned_plan"; then
            echo "provisioned plan reuses the legacy registry bootstrap:" >&2
            grep -nE 'registry' "$provisioned_plan" >&2
            exit 1
          fi
```

- [ ] **Step 5: Verify the new assertion passes on the real plan and fails on a planted one**

```bash
cd /Users/micheleciavotta/Downloads/nanolab
export NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas
p=$(mktemp)
uv run --package nanolab nanolab plan packages/nanolab/scenarios-v2/cli-contract-k8s.yaml \
  --environment packages/nanolab/environments/multipass.yaml >"$p"
grep -qE '^[0-9]+\.[a-z0-9-]*(acquire|ensure|configure)-[a-z0-9-]*registry' "$p" && echo "FAIL: trovato" || echo "ok: nessun registry"
printf '001.acquire-local-registry  Acquire local registry\n' > "$p.bad"
grep -qE '^[0-9]+\.[a-z0-9-]*(acquire|ensure|configure)-[a-z0-9-]*registry' "$p.bad" && echo "ok: rileva il caso pianto" || echo "FAIL: non rileva"
rm -f "$p" "$p.bad"
```

Expected: `ok: nessun registry` and `ok: rileva il caso pianto`.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: assert on names, not on versions and substrings"
```

---

### Task A2: One shell setting for the whole workflow

`set -euo pipefail` is repeated in two of four multi-line blocks. GitHub's default shell is `bash -e` without `pipefail`, so the two blocks that lack it silently pass when a command inside a pipe fails.

**Files:**
- Modify: `.github/workflows/ci.yml` (add `defaults`, remove two `set -euo pipefail` lines)

**Interfaces:**
- Consumes: nothing.
- Produces: every `run:` block in the workflow executes under `bash -euo pipefail`. Later tasks rely on this and must not re-add `set -euo pipefail`.

- [ ] **Step 1: Add the workflow-level default**

Immediately after the `permissions:` block in `.github/workflows/ci.yml`:

```yaml
defaults:
  run:
    # GitHub's default is `bash -e`, without pipefail: a failure inside a pipe
    # passes unnoticed. Setting it once beats repeating `set -euo pipefail` in
    # each block and forgetting it in half of them.
    shell: bash -euo pipefail {0}
```

- [ ] **Step 2: Remove the now-redundant lines**

Delete the `set -euo pipefail` line from the `Generate representative plans` step and from the `Run cli-container smoke test` step. Leave every other line untouched.

- [ ] **Step 3: Verify no block still sets it**

```bash
cd /Users/micheleciavotta/Downloads/nanolab
grep -c "set -euo pipefail" .github/workflows/ci.yml
grep -c "shell: bash -euo pipefail" .github/workflows/ci.yml
```

Expected: `0` and `1`.

- [ ] **Step 4: Verify the YAML still parses**

```bash
cd /Users/micheleciavotta/Downloads/nanolab
uv run --with pyyaml --no-project python -c "
import yaml; d=yaml.safe_load(open('.github/workflows/ci.yml'))
print('shell:', d['defaults']['run']['shell'])
print('steps:', len(d['jobs']['gate']['steps']))"
```

Expected: `shell: bash -euo pipefail {0}` and the step count unchanged from before the task.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: set the shell once, with pipefail"
```

---

### Task A3: A composite action for the workspace setup

Five setup steps precede every check. Task A4 splits the job, which would copy those five steps into each new job. Extract them first so the split costs nothing.

**Files:**
- Create: `.github/actions/setup-workspace/action.yml`
- Modify: `.github/workflows/ci.yml:18-61` (replace the five steps with one `uses:`)

**Interfaces:**
- Produces: a local composite action at `./.github/actions/setup-workspace` with one input, `nanofaas-token` (string, required: false). When the token is empty the action skips the private nanoFaaS checkout and sets the output `nanofaas` to `"false"`; otherwise `"true"`. Jobs that need `NANOFAAS_ROOT` gate on that output.

- [ ] **Step 1: Create the composite action**

Create `.github/actions/setup-workspace/action.yml`:

```yaml
name: Set up the nanolab workspace
description: >
  Checks out the pinned nanoFaaS source, installs the pinned JDK, uv and
  Python, and syncs the uv workspace. Every job needs all of it; written once
  so splitting the gate into parallel jobs costs nothing.

inputs:
  nanofaas-token:
    description: >
      Token for the private nanoFaaS checkout. Empty on pull requests from
      forks, where secrets are unavailable — the action then skips that
      checkout and reports it through the `nanofaas` output.
    required: false
    default: ""

outputs:
  nanofaas:
    description: "'true' when the nanoFaaS source is present, 'false' otherwise"
    value: ${{ steps.report.outputs.nanofaas }}

runs:
  using: composite
  steps:
    - name: Check out pinned nanoFaaS source
      if: ${{ inputs.nanofaas-token != '' }}
      uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0
      with:
        repository: miciav/nanofaas
        # nanofaas feat/spring-boot-4.1 at "Refresh GitNexus index metadata",
        # the checkout release work is developed against: Java 25 / GraalVM
        # 25.2.4, unified Java native builds, and the java-figlet,
        # python-mlimage and java-lite targets the image matrix now expands.
        # Tests read the version and the matrix from this checkout rather than
        # assuming them, so moving the pin needs no test edits.
        ref: f18f1be61491011224ed660eec142418ac9e3e26
        path: .nanofaas-source
        token: ${{ inputs.nanofaas-token }}

    - name: Install JDK
      uses: actions/setup-java@c5195efecf7bdfc987ee8bae7a71cb8b11521c00 # v4.7.1
      with:
        distribution: 'temurin'
        # Must track `javaVersion` in the pinned nanoFaaS gradle.properties.
        # The Gradle toolchain compiles to that release, but the cli-container
        # smoke test launches the control-plane jar with the ambient `java`
        # (plans/_local_control_plane.argv), so a lower runtime here builds
        # green and then dies with UnsupportedClassVersionError in one second.
        java-version: '25'
        cache: 'gradle'

    - name: Install uv
      uses: astral-sh/setup-uv@11f9893b081a58869d3b5fccaea48c9e9e46f990 # v8.3.2
      with:
        version: "0.11.32"
        enable-cache: true

    - name: Install Python
      shell: bash -euo pipefail {0}
      run: uv python install 3.12

    - name: Verify lock and sync workspace
      shell: bash -euo pipefail {0}
      run: |
        uv lock --check
        uv sync --locked --all-packages --all-groups

    - name: Report whether the nanoFaaS source is present
      id: report
      shell: bash -euo pipefail {0}
      run: |
        if [ -n "${{ inputs.nanofaas-token }}" ]; then
          echo "nanofaas=true" >>"$GITHUB_OUTPUT"
        else
          echo "nanofaas=false" >>"$GITHUB_OUTPUT"
          echo "::notice::No nanoFaaS token (fork pull request): steps needing NANOFAAS_ROOT will be skipped."
        fi
```

- [ ] **Step 2: Use it from the existing job**

In `.github/workflows/ci.yml`, replace the five steps from `Check out pinned nanoFaaS source` through `Verify lock and sync workspace` with:

```yaml
      - name: Set up the workspace
        id: sync
        uses: ./.github/actions/setup-workspace
        with:
          nanofaas-token: ${{ secrets.NANOFAAS_CHECKOUT_TOKEN }}
```

Keep the `Check out nanolab` step first — a composite action cannot run before the repository it lives in is checked out.

- [ ] **Step 3: Verify the YAML parses and the step count dropped by four**

```bash
cd /Users/micheleciavotta/Downloads/nanolab
uv run --with pyyaml --no-project python -c "
import yaml
d=yaml.safe_load(open('.github/workflows/ci.yml'))
a=yaml.safe_load(open('.github/actions/setup-workspace/action.yml'))
print('workflow steps:', len(d['jobs']['gate']['steps']))
print('action steps:', len(a['runs']['steps']))
print('uses composite:', a['runs']['using'])"
```

Expected: workflow steps 16, action steps 6, `composite`.

- [ ] **Step 4: Push and confirm CI is green**

```bash
git add .github/actions/setup-workspace/action.yml .github/workflows/ci.yml
git commit -m "ci: extract the workspace setup into a composite action"
git push origin main
gh run list --limit 1 --json status,conclusion --jq '.[0]'
```

Expected: the run completes with `"conclusion": "success"`. `steps.sync.outcome` still exists — the id is now on the composite step — so every `if:` keeps working.

---

### Task A4: Split the gate into parallel jobs

One job runs everything in series: the 15-minute container smoke test sits behind checks that take seconds. Splitting also lets fork pull requests get the feedback that does not need the private source.

**Files:**
- Modify: `.github/workflows/ci.yml` (replace the single `gate` job with five jobs)

**Interfaces:**
- Consumes: `./.github/actions/setup-workspace` from Task A3, including its `nanofaas` output.
- Produces: jobs named `checks`, `lint`, `package`, `plans`, `smoke`. Any later change adding a per-package check belongs in the `checks` matrix.

- [ ] **Step 1: Replace the job list**

Replace everything from `jobs:` to the end of `.github/workflows/ci.yml` with:

```yaml
concurrency:
  # One run per branch: pushes in quick succession supersede each other rather
  # than queueing, and the superseded one stops burning a runner.
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  # Per package, in parallel: the three packages' tests, type checks and import
  # contracts differ only by a name, and a matrix says that once.
  checks:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    strategy:
      fail-fast: false
      matrix:
        package: [nanolab, sonata-tasks, tui-toolkit]
    env:
      NANOFAAS_ROOT: ${{ github.workspace }}/.nanofaas-source
    steps:
      - name: Check out nanolab
        uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0

      - name: Set up the workspace
        id: sync
        uses: ./.github/actions/setup-workspace
        with:
          nanofaas-token: ${{ secrets.NANOFAAS_CHECKOUT_TOKEN }}

      # The tests read the pinned nanoFaaS checkout; the type check and the
      # import contracts do not, so a fork pull request still gets those two.
      - name: Run ${{ matrix.package }} tests
        if: ${{ !cancelled() && steps.sync.outcome == 'success' && steps.sync.outputs.nanofaas == 'true' }}
        run: uv run --locked --all-packages --all-groups pytest -c packages/${{ matrix.package }}/pyproject.toml packages/${{ matrix.package }}/tests

      - name: Type-check ${{ matrix.package }}
        if: ${{ !cancelled() && steps.sync.outcome == 'success' }}
        run: uv run --locked --all-packages --all-groups basedpyright --project packages/${{ matrix.package }}

      - name: Check ${{ matrix.package }} import contracts
        if: ${{ !cancelled() && steps.sync.outcome == 'success' }}
        run: uv run --locked --all-packages --all-groups lint-imports --config packages/${{ matrix.package }}/.importlinter --no-cache

  lint:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - name: Check out nanolab
        uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0

      - name: Set up the workspace
        uses: ./.github/actions/setup-workspace
        with:
          nanofaas-token: ${{ secrets.NANOFAAS_CHECKOUT_TOKEN }}

      - name: Lint
        run: uv run --locked --all-packages --all-groups ruff check packages

  package:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - name: Check out nanolab
        uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0

      - name: Set up the workspace
        uses: ./.github/actions/setup-workspace
        with:
          nanofaas-token: ${{ secrets.NANOFAAS_CHECKOUT_TOKEN }}

      - name: Build distributions
        id: build
        # No --clear: the runner checks out fresh, so there is nothing to clear,
        # and clearing races with the .gitignore uv keeps in the output directory
        # while four packages build into it. That race failed one run of PR #3
        # with "failed to open file dist/.gitignore" after every package had
        # already built successfully.
        run: uv build --all-packages --out-dir dist

      - name: Smoke-test installed wheels
        if: ${{ !cancelled() && steps.build.outcome == 'success' }}
        run: |
          uv venv .wheel-smoke
          # Glob, not names: the filenames carry the package version, and
          # repeating it here means a version bump breaks CI for no reason.
          uv pip install dist/*.whl --python .wheel-smoke/bin/python
          .wheel-smoke/bin/python -c "import nanolab, sonata_tasks, tui_toolkit"
          .wheel-smoke/bin/nanolab --help

  plans:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    env:
      NANOFAAS_ROOT: ${{ github.workspace }}/.nanofaas-source
    steps:
      - name: Check out nanolab
        uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0

      - name: Set up the workspace
        id: sync
        uses: ./.github/actions/setup-workspace
        with:
          nanofaas-token: ${{ secrets.NANOFAAS_CHECKOUT_TOKEN }}

      - name: Generate representative plans
        if: ${{ steps.sync.outputs.nanofaas == 'true' }}
        run: |
          uv run --locked --package nanolab nanolab plan packages/nanolab/scenarios-v2/deployment-lifecycle-container.yaml --environment packages/nanolab/environments/local.yaml
          uv run --locked --package nanolab nanolab plan packages/nanolab/scenarios-v2/deployment-lifecycle-k8s.yaml --environment packages/nanolab/environments/multipass.yaml
          uv run --locked --package nanolab nanolab plan packages/nanolab/scenarios-v2/cli-contract-k8s.yaml --control-plane-url http://control-plane.example:30080
          # Without the ordinal: the gate is that the plan releases the control
          # plane, not that it does so tenth. Pinning the number made this stale
          # the moment the plan grew a registry, and the failure stayed hidden
          # behind an earlier red step for days.
          uv run --locked --package nanolab nanolab plan packages/nanolab/scenarios-v2/cli-contract-container.yaml | grep -q 'release-local-control-plane'
          provisioned_plan="$(mktemp)"
          trap 'rm -f "$provisioned_plan"' EXIT
          uv run --locked --package nanolab nanolab plan packages/nanolab/scenarios-v2/cli-contract-k8s.yaml \
            --environment packages/nanolab/environments/multipass.yaml >"$provisioned_plan"
          # By position in the plan, not by ordinal: what matters is that the VM
          # is acquired before the chart needs it and released after, in reverse
          # order of acquisition. Pinning the numbers asserted the same thing
          # more weakly and went stale the moment a task was inserted.
          line_of() {
            grep -n -- "$1" "$provisioned_plan" | head -1 | cut -d: -f1
          }
          vm_acquired="$(line_of 'acquire-stack-vm')"
          helm_acquired="$(line_of 'acquire-control-plane-helm-release')"
          helm_released="$(line_of 'release-control-plane-helm-release')"
          vm_released="$(line_of 'release-stack-vm')"
          for step in "$vm_acquired" "$helm_acquired" "$helm_released" "$vm_released"; do
            if [ -z "$step" ]; then
              echo "provisioned plan is missing a VM or Helm step:" >&2
              cat "$provisioned_plan" >&2
              exit 1
            fi
          done
          test "$vm_acquired" -lt "$helm_acquired"
          test "$helm_released" -lt "$vm_released"
          # The gate is that no registry bootstrap task exists, not that the
          # word never appears: a substring match over the whole plan fails on
          # any future task that merely mentions a registry.
          if grep -qE '^[0-9]+\.[a-z0-9-]*(acquire|ensure|configure)-[a-z0-9-]*registry' "$provisioned_plan"; then
            echo "provisioned plan reuses the legacy registry bootstrap:" >&2
            grep -nE 'registry' "$provisioned_plan" >&2
            exit 1
          fi

  smoke:
    runs-on: ubuntu-latest
    timeout-minutes: 25
    env:
      NANOFAAS_ROOT: ${{ github.workspace }}/.nanofaas-source
    steps:
      - name: Check out nanolab
        uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0

      - name: Set up the workspace
        id: sync
        uses: ./.github/actions/setup-workspace
        with:
          nanofaas-token: ${{ secrets.NANOFAAS_CHECKOUT_TOKEN }}

      - name: Run cli-container smoke test
        if: ${{ steps.sync.outputs.nanofaas == 'true' }}
        run: |
          if curl -sS -m 2 http://127.0.0.1:18080/v1/functions >/dev/null 2>&1; then
            echo "API port 18080 is already in use" >&2
            exit 1
          fi
          if curl -sS -m 2 http://127.0.0.1:18081/actuator/health >/dev/null 2>&1; then
            echo "Management port 18081 is already in use" >&2
            exit 1
          fi
          uv run --locked --package nanolab \
            nanolab run packages/nanolab/scenarios-v2/cli-contract-container.yaml
          if curl -sS -m 2 http://127.0.0.1:18080/v1/functions >/dev/null 2>&1; then
            echo "API port 18080 remained open after cleanup" >&2
            exit 1
          fi
          if curl -sS -m 2 http://127.0.0.1:18081/actuator/health >/dev/null 2>&1; then
            echo "Management port 18081 remained open after cleanup" >&2
            exit 1
          fi
          remaining="$(docker ps --filter "name=nanofaas-" --format '{{.Names}}')"
          test -z "$remaining"
```

Keep the `name:`, `on:`, `permissions:` and `defaults:` blocks at the top of the file unchanged.

- [ ] **Step 2: Verify the structure**

```bash
cd /Users/micheleciavotta/Downloads/nanolab
uv run --with pyyaml --no-project python -c "
import yaml
d=yaml.safe_load(open('.github/workflows/ci.yml'))
for name, job in d['jobs'].items():
    m=job.get('strategy',{}).get('matrix',{})
    print(f\"{name:9} timeout={job['timeout-minutes']:>3}  steps={len(job['steps'])}  matrix={m or '-'}\")
print('concurrency:', d['concurrency'])"
```

Expected: five jobs, each with a timeout, `checks` carrying the three-package matrix, and a `concurrency` group with `cancel-in-progress: true`.

- [ ] **Step 3: Push and confirm every job reports separately**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: run the checks as parallel jobs"
git push origin main
until [ "$(gh run list --limit 1 --json status --jq '.[0].status')" = "completed" ]; do sleep 30; done
gh run view "$(gh run list --limit 1 --json databaseId --jq '.[0].databaseId')" --json jobs \
  --jq '.jobs[] | "\(.conclusion)  \(.name)"'
```

Expected: seven job results (`checks` three times, one per package, plus `lint`, `package`, `plans`, `smoke`), all `success`.

---

### Task A5: Check the Java version invariant instead of restating it

`java-version: '25'` is written in two repositories, and a comment says it must track `javaVersion` in the pinned nanoFaaS `gradle.properties`. Nothing verifies it. When it drifts the build goes green and dies at runtime with `UnsupportedClassVersionError`.

**Files:**
- Modify: `.github/actions/setup-workspace/action.yml` (add a verification step after the JDK install)

**Interfaces:**
- Consumes: the nanoFaaS checkout at `.nanofaas-source` from Task A3.
- Produces: nothing.

- [ ] **Step 1: Confirm the property exists and read its value**

```bash
grep -n "javaVersion" /Users/micheleciavotta/Downloads/mcFaas/gradle.properties
```

Expected: a line like `javaVersion=25`. If the property is absent or named differently, use the real name in Step 2 rather than inventing one.

- [ ] **Step 2: Add the check to the composite action**

Append to `.github/actions/setup-workspace/action.yml`, after the `Install JDK` step:

```yaml
    - name: Verify the JDK matches the pinned nanoFaaS source
      if: ${{ inputs.nanofaas-token != '' }}
      shell: bash -euo pipefail {0}
      run: |
        # The runtime java must not be older than what the source compiles to,
        # or the container smoke test dies with UnsupportedClassVersionError
        # after everything else has gone green.
        expected="$(grep -E '^javaVersion=' .nanofaas-source/gradle.properties | cut -d= -f2 | tr -d '[:space:]')"
        actual="$(java -version 2>&1 | head -1 | grep -oE '[0-9]+' | head -1)"
        if [ "$expected" != "$actual" ]; then
          echo "JDK mismatch: workflow installs $actual, pinned nanoFaaS compiles to $expected" >&2
          echo "Update java-version in .github/actions/setup-workspace/action.yml" >&2
          exit 1
        fi
        echo "JDK $actual matches the pinned nanoFaaS source"
```

- [ ] **Step 3: Verify the check locally against the real checkout**

```bash
cd /Users/micheleciavotta/Downloads/mcFaas
expected="$(grep -E '^javaVersion=' gradle.properties | cut -d= -f2 | tr -d '[:space:]')"
actual="$(java -version 2>&1 | head -1 | grep -oE '[0-9]+' | head -1)"
echo "atteso=$expected installato=$actual"
```

Expected: the two agree, or the mismatch is real and the workflow's `java-version` needs updating before continuing.

- [ ] **Step 4: Commit**

```bash
cd /Users/micheleciavotta/Downloads/nanolab
git add .github/actions/setup-workspace/action.yml
git commit -m "ci: verify the JDK against the pinned source instead of restating it"
```

---

### Task A6: Bring the nanoFaaS workflow up to the same standard

`mcFaas/.github/workflows/gitops.yml` has no `permissions`, no pinned action SHAs, an unpinned Python, no timeouts and no concurrency group — and its name promises a deployment pipeline it does not contain.

**Files:**
- Modify: `/Users/micheleciavotta/Downloads/mcFaas/.github/workflows/gitops.yml`

**Interfaces:**
- Consumes: nothing from Part A's other tasks. This repository has no composite action; three jobs with three different toolchains share too little to extract one.
- Produces: nothing.

- [ ] **Step 1: Resolve the SHAs for the five actions**

```bash
for a in actions/checkout@v4 actions/setup-java@v4 azure/setup-helm@v4.3.1 astral-sh/setup-uv@v5 Swatinem/rust-cache@v2 dtolnay/rust-toolchain@stable; do
  repo="${a%@*}"; ref="${a#*@}"
  sha=$(gh api "repos/$repo/commits/$ref" --jq '.sha' 2>/dev/null)
  echo "$repo@$sha  # $ref"
done
```

Record the output: Step 2 needs those SHAs verbatim.

- [ ] **Step 2: Rewrite the workflow header and jobs**

Replace the top of `gitops.yml` (through `jobs:`) with:

```yaml
name: nanoFaaS CI

on:
  push:
    branches: ["main"]
  pull_request:
    branches: ["main"]

permissions:
  contents: read

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

defaults:
  run:
    shell: bash -euo pipefail {0}

jobs:
```

Then, in each job: add `timeout-minutes` (`test-java: 30`, `test-python: 15`, `test-watchdog: 20` — the watchdog step's own 15-minute timeout stays), replace every `uses:` with the `repo@sha # tag` form recorded in Step 1, and pin the Python version:

```yaml
      - name: Set up Python
        run: uv python install 3.12
```

- [ ] **Step 3: Verify the file parses and nothing floats**

```bash
cd /Users/micheleciavotta/Downloads/mcFaas
uv run --with pyyaml --no-project --directory /Users/micheleciavotta/Downloads/nanolab python -c "
import yaml
d=yaml.safe_load(open('/Users/micheleciavotta/Downloads/mcFaas/.github/workflows/gitops.yml'))
print('name:', d['name']); print('permissions:', d.get('permissions'))
for n,j in d['jobs'].items(): print(f'  {n}: timeout={j.get(\"timeout-minutes\")}')"
grep -E "uses:" .github/workflows/gitops.yml | grep -vc "@[0-9a-f]\{40\}"
```

Expected: the summary prints a timeout for all three jobs, and the last command prints `0` — no `uses:` left on a floating ref.

- [ ] **Step 4: Commit and push**

```bash
cd /Users/micheleciavotta/Downloads/mcFaas
git add .github/workflows/gitops.yml
git commit -m "ci: pin the actions, the Python and the permissions"
git push origin main
gh run list --repo miciav/nanofaas --limit 1 --json status,conclusion --jq '.[0]'
```

Expected: the run completes `success`.

---

# Part B — Sonata plan builders

### Task B1: Stop rebinding `remote` from bool to path

`plans/loadtest.py:209` binds `remote` to a boolean, line 218 rebinds the same name to a `PurePosixPath`, and line 346 reads it as a truth value again. It works only because every `PurePosixPath` is truthy.

**Files:**
- Modify: `packages/nanolab/src/nanolab/plans/loadtest.py:209-236`
- Test: `packages/nanolab/tests/plans/test_loadtest.py`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing. Local rename only.

- [ ] **Step 1: Write the failing test**

Add to `packages/nanolab/tests/plans/test_loadtest.py`:

```python
def test_a_remote_run_dir_must_be_an_absolute_run_child(tmp_path: Path) -> None:
    """The remote run directory is validated by shape; a relative one is refused
    with a message about the directory, not a type error about a boolean."""
    executor = RecordingExecutor()

    with pytest.raises(ValueError, match="absolute run-N child"):
        build_loadtest_plan(
            SCENARIO,
            EnvironmentConfig.model_validate(
                {"provider": "multipass", "roles": {"stack": {"name": "nanofaas-stack"}}}
            ),
            RoleBindings(host=executor, stack=executor),
            control_plane_url="http://stack:30080",
            prometheus_client=NoopPrometheus(),
            run_dir=tmp_path,
            remote_run_dir=Path("nanofaas-release/v1/benchmarks/run-1"),
        )
```

- [ ] **Step 2: Run it and watch it pass for the wrong reason**

Run: `NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --locked pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/plans/test_loadtest.py::test_a_remote_run_dir_must_be_an_absolute_run_child -v --no-cov`

Expected: PASS. This test guards the behaviour the rename must preserve; it is the safety net, not the driver.

- [ ] **Step 3: Rename the rebound variable**

In `packages/nanolab/src/nanolab/plans/loadtest.py`, inside the `if remote:` block, replace the four uses of the rebound name:

```python
        if remote_run_dir is not None:
            requested = PurePosixPath(str(remote_run_dir))
            selected_home = PurePosixPath(home)
            try:
                relative = requested.relative_to(selected_home)
            except ValueError:
                relative = PurePosixPath()
            parts = relative.parts
            run_number = requested.name.removeprefix("run-")
            if (
                not requested.is_absolute()
                or ".." in requested.parts
                or len(parts) != 4
                or parts[0] != "nanofaas-release"
                or not parts[1]
                or parts[2] != "benchmarks"
                or not run_number.isdigit()
                or int(run_number) < 1
            ):
                raise ValueError("remote run directory must be an absolute run-N child")
```

`remote` now keeps its boolean meaning for the whole function, including the `if remote:` at line 346.

- [ ] **Step 4: Verify the name is never rebound**

```bash
cd /Users/micheleciavotta/Downloads/nanolab
grep -n "^\s*remote = \|^\s*remote=" packages/nanolab/src/nanolab/plans/loadtest.py
```

Expected: exactly one line, the boolean assignment.

- [ ] **Step 5: Run the suite**

Run: `NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --locked pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q`

Expected: same result as before the task (one pre-existing failure in `test_release.py` if the nanoFaaS tree is dirty; nothing new).

- [ ] **Step 6: Commit**

```bash
git add packages/nanolab/src/nanolab/plans/loadtest.py packages/nanolab/tests/plans/test_loadtest.py
git commit -m "fix: keep remote a boolean for the whole function"
```

---

### Task B2: One `_set_args`, publicly named

Three identical copies build Helm `--set` arguments: `plans/validate.py:135`, `plans/cli.py:180`, `components/helm.py:99`. The one that survives belongs beside the other Helm helpers and must be public, or the callers repeat today's mistake of importing a private name across modules.

**Files:**
- Modify: `packages/sonata-tasks/src/sonata_tasks/components/helm.py:99` (rename to a public name)
- Modify: `packages/nanolab/src/nanolab/plans/validate.py` (delete the copy, import the shared one)
- Modify: `packages/nanolab/src/nanolab/plans/cli.py` (delete the copy, import the shared one)
- Modify: `packages/nanolab/src/nanolab/plans/loadtest.py`, `packages/nanolab/src/nanolab/plans/offload_loadtest.py` (import from the new home)
- Test: `packages/sonata-tasks/tests/components/test_helm.py`

**Interfaces:**
- Produces: `sonata_tasks.components.helm.helm_set_args(values: Mapping[str, str]) -> tuple[str, ...]`, returning `("--set", "k=v", ...)` in insertion order. Tasks B4 and B5 import this name.

- [ ] **Step 1: Write the failing test**

Add to `packages/sonata-tasks/tests/components/test_helm.py`:

```python
def test_helm_set_args_pairs_every_value_with_its_flag() -> None:
    assert helm_mod.helm_set_args({"a": "1", "b": "2"}) == (
        "--set", "a=1", "--set", "b=2",
    )


def test_helm_set_args_keeps_insertion_order() -> None:
    """Helm applies later --set values over earlier ones, so order is meaning."""
    assert helm_mod.helm_set_args({"z": "1", "a": "2"})[1::2] == ("z=1", "a=2")
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run --locked pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/components/test_helm.py -q --no-cov`

Expected: FAIL with `AttributeError: module ... has no attribute 'helm_set_args'`.

- [ ] **Step 3: Rename the surviving copy**

In `packages/sonata-tasks/src/sonata_tasks/components/helm.py`, rename `_set_args` to `helm_set_args`, keep the body, and update its two internal call sites in that file:

```python
def helm_set_args(values: Mapping[str, str]) -> tuple[str, ...]:
    """Turn a value map into Helm's `--set key=value` arguments.

    Public and shared: four plan builders need it, and the private copies they
    each kept had to be corrected in three places at once.
    """
    args: list[str] = []
    for key, value in values.items():
        args.extend(["--set", f"{key}={value}"])
    return tuple(args)
```

- [ ] **Step 4: Run the test**

Run: `uv run --locked pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/components/test_helm.py -q --no-cov`

Expected: PASS.

- [ ] **Step 5: Delete the two copies and import the shared one**

In `packages/nanolab/src/nanolab/plans/validate.py` and `packages/nanolab/src/nanolab/plans/cli.py`, delete the local `def _set_args(...)`. In all four plan modules that use it (`validate.py`, `cli.py`, `loadtest.py`, `offload_loadtest.py`) add:

```python
from sonata_tasks.components.helm import helm_set_args
```

and replace every `_set_args(` call with `helm_set_args(`. In `loadtest.py` and `offload_loadtest.py` also remove `_set_args` from the `from nanolab.plans.validate import ...` line.

- [ ] **Step 6: Verify one definition remains**

```bash
cd /Users/micheleciavotta/Downloads/nanolab
grep -rn "def helm_set_args\|def _set_args" packages/*/src
grep -rc "helm_set_args" packages/nanolab/src/nanolab/plans/*.py | grep -v ":0"
```

Expected: exactly one definition, and four plan modules referencing it.

- [ ] **Step 7: Run both suites and the checker**

```bash
uv run --locked pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -q
NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --locked pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
uv run --locked ruff check packages
uv run --locked --all-packages --all-groups basedpyright --project packages/nanolab
```

Expected: no new failures, `All checks passed!`, `0 errors`.

- [ ] **Step 8: Commit**

```bash
git add packages/sonata-tasks packages/nanolab
git commit -m "refactor: one helm_set_args, named as the shared thing it is"
```

---

### Task B3: One `_home`

`plans/loadtest.py:103` and `cli/execution.py:204` encode the same rule — `/root` for root, `/home/<user>` otherwise — with different signatures. The rule already exists in `sonata_tasks.vm.models.vm_remote_home` for `VmRequest`; the plan layer needs the same answer from a `RoleTarget`.

**Files:**
- Modify: `packages/nanolab/src/nanolab/config/environment.py` (add the method)
- Modify: `packages/nanolab/src/nanolab/plans/loadtest.py:103` (delete `_home`, call the method)
- Modify: `packages/nanolab/src/nanolab/cli/execution.py:204` (delete `_home`, call the method)
- Test: `packages/nanolab/tests/config/test_environment.py` (create if absent)

**Interfaces:**
- Produces: `RoleTarget.remote_home` — a read-only property returning `str`: the explicit `home` when set, else `/root` for user `root`, else `/home/<user>`.

- [ ] **Step 1: Write the failing test**

Create or extend `packages/nanolab/tests/config/test_environment.py`:

```python
from nanolab.config.environment import RoleTarget


def test_remote_home_defaults_to_the_users_home() -> None:
    assert RoleTarget(user="ubuntu").remote_home == "/home/ubuntu"


def test_remote_home_is_slash_root_for_root() -> None:
    assert RoleTarget(user="root").remote_home == "/root"


def test_an_explicit_home_wins() -> None:
    assert RoleTarget(user="ubuntu", home="/srv/nanofaas").remote_home == "/srv/nanofaas"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --locked pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/config/test_environment.py -q --no-cov`

Expected: FAIL with `AttributeError: 'RoleTarget' object has no attribute 'remote_home'`.

- [ ] **Step 3: Add the property to the model that owns the data**

In `packages/nanolab/src/nanolab/config/environment.py`, inside `class RoleTarget`:

```python
    @property
    def remote_home(self) -> str:
        """Where this role's user lives on its machine.

        On the model rather than in a helper: two modules had grown their own
        copy of the rule with different signatures, and neither could be found
        from the other.
        """
        return self.home or ("/root" if self.user == "root" else f"/home/{self.user}")
```

- [ ] **Step 4: Run the test**

Run: `NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --locked pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/config/test_environment.py -q --no-cov`

Expected: PASS.

- [ ] **Step 5: Delete both copies**

In `packages/nanolab/src/nanolab/cli/execution.py`, delete `def _home(target: RoleTarget) -> str:` and replace its three call sites `_home(target)` / `_home(self._target)` with `target.remote_home` / `self._target.remote_home`.

In `packages/nanolab/src/nanolab/plans/loadtest.py`, delete `def _home(user: str, explicit: str | None) -> str:` and replace the call `home = _home(role_target.user, role_target.home)` with `home = role_target.remote_home`.

- [ ] **Step 6: Verify no copies remain**

```bash
cd /Users/micheleciavotta/Downloads/nanolab
grep -rn "def _home" packages/nanolab/src
grep -rn "remote_home" packages/nanolab/src | wc -l
```

Expected: no `def _home`, and at least four references to `remote_home`.

- [ ] **Step 7: Run the suite and the checker**

```bash
NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --locked pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
uv run --locked --all-packages --all-groups basedpyright --project packages/nanolab
```

Expected: no new failures, `0 errors`.

- [ ] **Step 8: Commit**

```bash
git add packages/nanolab
git commit -m "refactor: ask the role target where its home is"
```

---

### Task B4: A shared module instead of private imports from `validate`

Four plan modules import `_resolve_function`, `_sonata_function` and `_ResolvedFunction` from `nanolab.plans.validate`. The underscore says private; four importers say shared library. Move them to a module that admits what they are.

**Files:**
- Create: `packages/nanolab/src/nanolab/plans/functions.py`
- Modify: `packages/nanolab/src/nanolab/plans/validate.py:22-133` (delete the moved definitions, import them)
- Modify: `packages/nanolab/src/nanolab/plans/cli.py:36`, `loadtest.py:47`, `offload.py:19`, `offload_loadtest.py:33` (import from the new module)
- Test: `packages/nanolab/tests/plans/test_functions.py`

**Interfaces:**
- Produces, all public, in `nanolab.plans.functions`:
  - `ResolvedFunction` — frozen dataclass, the fields the current `_ResolvedFunction` has, unchanged.
  - `resolve_function(config: ScenarioConfig, key: str, *, tool_root: Path | None = None) -> ResolvedFunction`
  - `sonata_function(resolved: ResolvedFunction) -> SonataFunction`
- Task B5 imports `resolve_function` from here.

- [ ] **Step 1: Write the failing test**

Create `packages/nanolab/tests/plans/test_functions.py`:

```python
from __future__ import annotations

from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.functions import ResolvedFunction, resolve_function, sonata_function


def _scenario() -> ScenarioConfig:
    return ScenarioConfig.model_validate(
        {"workflow": "validate", "backend": "k8s", "functions": ["word-stats-java"]}
    )


def test_resolve_function_returns_the_shared_shape() -> None:
    resolved = resolve_function(_scenario(), "word-stats-java")

    assert isinstance(resolved, ResolvedFunction)
    assert resolved.key == "word-stats-java"
    assert resolved.image
    assert resolved.build_argv


def test_sonata_function_carries_the_name_and_image_across() -> None:
    resolved = resolve_function(_scenario(), "word-stats-java")

    task_shape = sonata_function(resolved)

    assert task_shape.name == resolved.name
    assert task_shape.image == resolved.image
```

- [ ] **Step 2: Run it and watch it fail**

Run: `NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --locked pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/plans/test_functions.py -q --no-cov`

Expected: FAIL with `ModuleNotFoundError: No module named 'nanolab.plans.functions'`.

- [ ] **Step 3: Create the module by moving the definitions**

Create `packages/nanolab/src/nanolab/plans/functions.py` containing, moved verbatim from `validate.py` and renamed without the leading underscore: the `_ResolvedFunction` dataclass (as `ResolvedFunction`), `_function_image`, `_build_argv`, `_image_build_argv`, `_function_name`, `_payload` (these five stay private to the new module), `_resolve_function` (as `resolve_function`) and `_sonata_function` (as `sonata_function`). Carry every import those functions need. Head the module with:

```python
"""How a scenario's function key becomes something a task can run.

Four plan builders need this, which is why it is a module of its own: it used
to live in `validate` under private names that everyone imported anyway, so the
underscore documented an intent the code contradicted.
"""
```

- [ ] **Step 4: Run the new test**

Run: `NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --locked pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/plans/test_functions.py -q --no-cov`

Expected: PASS.

- [ ] **Step 5: Point every importer at the new module**

In `validate.py`, delete the moved definitions and add `from nanolab.plans.functions import ResolvedFunction, resolve_function, sonata_function`; replace internal uses of the old names. In `cli.py`, `loadtest.py`, `offload.py` and `offload_loadtest.py`, replace `from nanolab.plans.validate import _resolve_function, ...` with the equivalent import from `nanolab.plans.functions`, and update the call sites.

- [ ] **Step 6: Verify no plan imports a private name from another plan**

```bash
cd /Users/micheleciavotta/Downloads/nanolab
grep -rn "^from nanolab.plans\.[a-z_]* import" packages/nanolab/src/nanolab/plans/*.py | grep " _"
```

Expected: no output.

- [ ] **Step 7: Run everything**

```bash
NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --locked pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
uv run --locked ruff check packages
uv run --locked --all-packages --all-groups basedpyright --project packages/nanolab
uv run --locked --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
```

Expected: no new failures, `All checks passed!`, `0 errors`, `Contracts: 3 kept, 0 broken`.

- [ ] **Step 8: Commit**

```bash
git add packages/nanolab
git commit -m "refactor: give the shared function helpers their own module"
```

---

### Task B5: Name the deployment constants

`nanofaas-e2e` appears in ten files, `localhost:5000` in ten, the node ports in five to seven each. Each occurrence is a copy of a decision made elsewhere.

**Files:**
- Create: `packages/sonata-tasks/src/sonata_tasks/deployment.py`
- Modify: every file that currently spells one of the literals (find them in Step 1)
- Test: `packages/sonata-tasks/tests/test_deployment.py`

**Interfaces:**
- Produces, in `sonata_tasks.deployment`:
  - `DEFAULT_NAMESPACE: str = "nanofaas-e2e"`
  - `LOCAL_REGISTRY: str = "localhost:5000"`
  - `CONTROL_PLANE_NODE_PORT: int = 30080`
  - `PROMETHEUS_NODE_PORT: int = 30090`
  - `LOCAL_CONTROL_PLANE_API_PORT: int = 18080`
  - `LOCAL_CONTROL_PLANE_MANAGEMENT_PORT: int = 18081`
- `sonata_tasks.platform.CONTROL_PLANE_SERVICE` and `CONTROL_PLANE_PORT` stay where they are: they describe the in-cluster Service, not the lab deployment, and moving them would widen this task without cause.

- [ ] **Step 1: Inventory every occurrence before touching anything**

```bash
cd /Users/micheleciavotta/Downloads/nanolab
for s in "nanofaas-e2e" "localhost:5000" 30080 30090 18080 18081; do
  echo "== $s"; grep -rn -- "$s" packages/*/src | sed 's|packages/||'
done
```

Keep this output: Step 4 replaces exactly these, and no others. Occurrences inside a docstring or a comment that quotes an example stay as they are — they are prose, not configuration.

- [ ] **Step 2: Write the failing test**

Create `packages/sonata-tasks/tests/test_deployment.py`:

```python
from sonata_tasks.deployment import (
    CONTROL_PLANE_NODE_PORT,
    DEFAULT_NAMESPACE,
    LOCAL_CONTROL_PLANE_API_PORT,
    LOCAL_CONTROL_PLANE_MANAGEMENT_PORT,
    LOCAL_REGISTRY,
    PROMETHEUS_NODE_PORT,
)


def test_the_lab_deployment_constants_have_their_documented_values() -> None:
    """Pinned deliberately: these are the values the scenarios, the Helm values
    and the CI assertions all assume, and a silent change would move them apart."""
    assert DEFAULT_NAMESPACE == "nanofaas-e2e"
    assert LOCAL_REGISTRY == "localhost:5000"
    assert CONTROL_PLANE_NODE_PORT == 30080
    assert PROMETHEUS_NODE_PORT == 30090
    assert LOCAL_CONTROL_PLANE_API_PORT == 18080
    assert LOCAL_CONTROL_PLANE_MANAGEMENT_PORT == 18081


def test_the_node_ports_are_distinct() -> None:
    ports = {
        CONTROL_PLANE_NODE_PORT,
        PROMETHEUS_NODE_PORT,
        LOCAL_CONTROL_PLANE_API_PORT,
        LOCAL_CONTROL_PLANE_MANAGEMENT_PORT,
    }
    assert len(ports) == 4
```

- [ ] **Step 3: Run it and watch it fail**

Run: `uv run --locked pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/test_deployment.py -q --no-cov`

Expected: FAIL with `ModuleNotFoundError: No module named 'sonata_tasks.deployment'`.

- [ ] **Step 4: Create the module**

Create `packages/sonata-tasks/src/sonata_tasks/deployment.py`:

```python
"""Where the lab deployment lives: namespace, registry and node ports.

One home per value. Spelled out in ten files each, they were ten copies of a
decision made once — the shape that let a CI expectation and a dependency pin
drift from the thing they described.
"""

from __future__ import annotations

DEFAULT_NAMESPACE = "nanofaas-e2e"
LOCAL_REGISTRY = "localhost:5000"

# NodePorts the stack VM publishes.
CONTROL_PLANE_NODE_PORT = 30080
PROMETHEUS_NODE_PORT = 30090

# Ports the container backend's local control plane binds on the host.
LOCAL_CONTROL_PLANE_API_PORT = 18080
LOCAL_CONTROL_PLANE_MANAGEMENT_PORT = 18081
```

- [ ] **Step 5: Run the test**

Run: `uv run --locked pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/test_deployment.py -q --no-cov`

Expected: PASS.

- [ ] **Step 6: Replace the literals, one file at a time**

Work through the inventory from Step 1. For each file, import what it needs and replace the literal. An f-string keeps reading naturally:

```python
from sonata_tasks.deployment import CONTROL_PLANE_NODE_PORT, PROMETHEUS_NODE_PORT

    return (
        control_plane_url or f"http://{host}:{CONTROL_PLANE_NODE_PORT}",
        prometheus_url or f"http://{host}:{PROMETHEUS_NODE_PORT}",
    )
```

After each file, run that package's tests before moving to the next:

```bash
NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --locked pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
```

- [ ] **Step 7: Verify only the intended literals remain**

```bash
cd /Users/micheleciavotta/Downloads/nanolab
for s in "nanofaas-e2e" "localhost:5000" 30080 30090 18080 18081; do
  printf "%-16s %s\n" "$s" "$(grep -rn -- "$s" packages/*/src | grep -v "deployment.py" | grep -vc "#")"
done
```

Expected: each count is far below the inventory in Step 1. Any survivor must be justified in the commit message — a default in a Pydantic model that the constants module cannot import without a cycle is a legitimate survivor; a forgotten literal is not.

- [ ] **Step 8: Run everything**

```bash
uv run --locked pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -q
NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --locked pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
uv run --locked ruff check packages
uv run --locked --all-packages --all-groups basedpyright --project packages/sonata-tasks
uv run --locked --all-packages --all-groups basedpyright --project packages/nanolab
```

Expected: no new failures, `All checks passed!`, `0 errors` twice.

- [ ] **Step 9: Commit**

```bash
git add packages
git commit -m "refactor: name the namespace, the registry and the ports"
```

---

### Task B6: Split `build_release_workflow` into phase builders

`plans/release.py` is 967 lines with seven top-level definitions; `build_release_workflow` is 703 of them, with 51 first-level locals, 9 nested functions and six levels of indentation. It is the most expensive unit in the codebase to change and the least urgent to touch — so it is split last, one phase at a time, with the tests already green.

**Files:**
- Modify: `packages/nanolab/src/nanolab/plans/release.py`
- Create: `packages/nanolab/src/nanolab/plans/release_phases.py`
- Test: `packages/nanolab/tests/plans/test_release.py` (existing; must keep passing unchanged)

**Interfaces:**
- Produces, in `nanolab.plans.release_phases`, one function per phase, each taking the values it needs and returning the tasks or resources it contributes. Exact signatures are decided in Step 2 from the real code, not guessed here.

- [ ] **Step 1: Record the behaviour that must not change**

```bash
cd /Users/micheleciavotta/Downloads/nanolab
NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --locked pytest \
  -c packages/nanolab/pyproject.toml packages/nanolab/tests/plans/test_release.py -q --no-cov \
  | tail -2
grep -c "def test_" packages/nanolab/tests/plans/test_release.py
```

Expected: a passing count (one pre-existing failure if the nanoFaaS tree is dirty — commit or stash it first, and note the number of tests). That number must be identical after every step below.

- [ ] **Step 2: Map the phases before moving a line**

```bash
cd /Users/micheleciavotta/Downloads/nanolab
sed -n '252,955p' packages/nanolab/src/nanolab/plans/release.py \
  | grep -nE "^    [a-z_]+ = |^    def |# ---|workflow\." | head -60
```

Write the phase list into the commit message of Step 7. Expect roughly: source tests, AMD64 image build, SBOM, publication, attestation, release record, documentation finalisation. Each phase is a contiguous run of statements whose locals are used only inside it — the ones that are not are the seams to keep as parameters.

- [ ] **Step 3: Extract the first phase only**

Create `packages/nanolab/src/nanolab/plans/release_phases.py` with a module docstring:

```python
"""One function per release phase.

`build_release_workflow` reached 703 lines and 51 live locals, which is more
than a reader can hold at once and is why every change there costs a full
re-read. The phases were already there in the comments; this gives them names.
"""
```

Move the first phase's statements into a function whose parameters are exactly the locals it reads from outside itself and whose return value is exactly what the rest of the function uses afterwards. Call it from `build_release_workflow` in place of the moved lines.

- [ ] **Step 4: Run the release tests after that one phase**

Run: `NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --locked pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/plans/test_release.py -q --no-cov`

Expected: the same passing count as Step 1. If it differs, revert this step and re-cut the seam — a phase whose extraction changes behaviour was not a phase.

- [ ] **Step 5: Commit that phase alone**

```bash
git add packages/nanolab/src/nanolab/plans/release.py packages/nanolab/src/nanolab/plans/release_phases.py
git commit -m "refactor: extract the <phase name> release phase"
```

- [ ] **Step 6: Repeat steps 3 to 5 for each remaining phase**

One phase per commit. Never two. After each, `build_release_workflow` is shorter and still passes; if a phase resists extraction because its locals are read three phases later, leave it in place and say so in the final commit message — a seam that does not exist must not be invented.

- [ ] **Step 7: Confirm the result and run everything**

```bash
cd /Users/micheleciavotta/Downloads/nanolab
awk '/^def build_release_workflow/,/^def [a-z_]+\(.*\).*:$/' packages/nanolab/src/nanolab/plans/release.py | wc -l
wc -l packages/nanolab/src/nanolab/plans/release.py packages/nanolab/src/nanolab/plans/release_phases.py
NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --locked pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
uv run --locked ruff check packages
uv run --locked --all-packages --all-groups basedpyright --project packages/nanolab
uv run --locked --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
```

Expected: `build_release_workflow` well under 200 lines, the test count unchanged from Step 1, `All checks passed!`, `0 errors`, `Contracts: 3 kept, 0 broken`.

---

## Execution order and independence

Part A and Part B touch disjoint files and can run in either order or in parallel by two people.

Within Part A the order matters: A3 (composite action) must precede A4 (job split), or the split copies five setup steps into five jobs. A1 and A2 are independent of both and can go first, which is what makes A4's large diff a pure move.

Within Part B the order is by cost: B1 is ten minutes and removes a latent bug; B2, B3 and B4 are mechanical and independent of each other; B5 touches the most files and should follow B2 and B4 so it does not collide with their imports; B6 is last and is the only task whose steps repeat per phase.

## What this plan deliberately does not do

- **Static analysis for the nanoFaaS repository.** It has none, while nanolab has three layers of it. Adding ruff, a Java linter and Rust clippy across three toolchains is its own project with its own decisions, not a task appended here.
- **Moving `CONTROL_PLANE_SERVICE` / `CONTROL_PLANE_PORT`** out of `platform.py`. They describe the in-cluster Service; the constants in B5 describe the lab deployment. Merging them would conflate two ideas that happen to be near each other.
- **Reducing `build_loadtest_plan`'s 14 parameters.** They are 14 genuinely different inputs, and the measurement that would justify a change — is any caller passing a set that suggests a missing object? — has not been made.
- **Removing the repeated `uv run --locked --all-packages --all-groups` prefix.** The matrix in A4 takes it from ten occurrences to about five, one per job, which is where it stops being duplication and starts being how each job says what it runs. Hiding it in an environment variable would trade five readable lines for one indirection.

## One task is deliberately under-specified

Task B6 gives the procedure, the verification and the commit discipline, but not the exact signatures of the phase functions: those follow from where the 51 locals actually stop being read, and that seam can only be cut with the file open. Every other task in this plan can be executed from the text alone; B6 requires reading `release.py` first, and Step 2 is that reading. Inventing signatures here would produce parameters that do not match the code and a plan that lies with confidence.
