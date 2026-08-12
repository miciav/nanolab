# Provider-Owned Provisioning Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove the user-facing `--provision` switch: managed providers ensure and release their VMs through the existing lifecycle, while an `external`/SSH environment only verifies and uses its host.

**Architecture:** Keep the existing `provision_environment()` bridge for non-release workflows; it already delegates VM lifecycle to `sonata_tasks.provisioning.provision_roles`, whose `external` lifecycle SSH-checks and deliberately emits no destroy task. Make this bridge automatic for every non-local environment instead of conditional on a CLI flag. Release and CLI/k8s already own their VM lifecycle in their Sonata workflow; make that their unconditional non-local path. Do not introduce a second provider abstraction or migrate unrelated workflow builders in this change.

**Tech Stack:** Python 3.12, Typer, Sonata Engine/Tasks, pytest.

---

## Decisions locked by this plan

- `run` and `plan` no longer expose or accept `--provision`.
- `provider: local` remains host-local and acquires no VM.
- `provider: multipass`, `azure`, and `proxmox` automatically ensure the requested role VM(s); normal workflow cleanup releases managed VMs unless `--keep` is supplied.
- `provider: external` still enters the same lifecycle, but `EnsureVmRunning` is an SSH availability check and `DestroyVm` is omitted. No user-owned VM is created or destroyed.
- A release starts fresh unless `--resume` is present. The fresh run owns the same resource graph regardless of the old flag.
- A fresh release may not rename a non-empty run directory before its release lock is held. It must reject a journal that still retains resources, telling the operator to run `--teardown` or `--resume`; it must not guess which superseded journal to destroy.

## Task 1: Characterize automatic lifecycle routing

**Files:**

- Modify: `packages/nanolab/tests/cli/test_release_command.py`
- Modify: `packages/nanolab/tests/test_provisioning.py`
- Modify: `packages/nanolab/tests/plans/test_cli.py`

**Step 1: Write failing CLI tests.**

Replace fresh-release invocations such as `invoke("--provision")` with `invoke()`. Add a test that passes `--provision` to both `run` and `plan` and asserts Typer rejects it as an unknown option. Add a non-release managed-environment `run` harness assertion that provisioning enters automatically, and preserve the existing external assertion that `ensure` is called but `teardown` is not.

**Step 2: Run the focused tests and verify the new assertions fail.**

Run: `uv run pytest packages/nanolab/tests/cli/test_release_command.py packages/nanolab/tests/test_provisioning.py packages/nanolab/tests/plans/test_cli.py -q`

Expected: failures only where the CLI still requires or recognizes `--provision`.

**Step 3: Commit the characterization tests.**

```bash
git add packages/nanolab/tests/cli/test_release_command.py packages/nanolab/tests/test_provisioning.py packages/nanolab/tests/plans/test_cli.py
git commit -m "test: specify provider-owned provisioning"
```

## Task 2: Delete `--provision` and make the existing routes unconditional

**Files:**

- Modify: `packages/nanolab/src/nanolab/cli/product.py:84-129,241-272,387-710`
- Modify: `packages/nanolab/src/nanolab/plans/cli.py:218-374`
- Modify: `packages/nanolab/tests/cli/test_release_command.py`
- Modify: `packages/nanolab/tests/plans/test_cli.py`

**Step 1: Remove the flag plumbing.**

Delete the `provision` argument from `_workflow`, `_require_cli_endpoint`, `_cli_provisioned`, `_uses_legacy_provisioning`, `run_command`, and `plan_command`. Remove the local-provider rejection and all error text that mentions `--provision`.

Replace the split with these direct rules:

```python
managed_environment = environment_config.provider != "local"
if scenario_config.workflow != "release" and managed_environment:
    provisioning = provision_environment(..., keep=keep)
elif release_request is not None:
    provisioning = release_run_lock(...)
else:
    provisioning = nullcontext()
```

Keep `provider: external` in the automatic branch: `provision_roles()` already skips its destroy task.

**Step 2: Make CLI/k8s decide from the environment, not the flag.**

Rename `_build_provisioned_k8s_plan` to `_build_k8s_plan`. Remove its `--provision` error messages. In `build_cli_plan`, remove `provision`; when `config.backend == "k8s"` and `environment.provider != "local"`, always build the VM/Helm workflow. Keep the existing endpoint-required path only for a local environment targeting an already-running remote endpoint, if that configuration is supported; otherwise reject that invalid pairing clearly.

The minimal branch is:

```python
if config.backend == "k8s" and environment is not None and environment.provider != "local":
    return _build_k8s_plan(...)
```

This reuses the existing `vm_resource`/`VmLifecycleAdapter`, so Azure/Proxmox create or converge and `external` verifies SSH without teardown.

**Step 3: Update the focused tests.**

Run: `uv run pytest packages/nanolab/tests/cli/test_release_command.py packages/nanolab/tests/plans/test_cli.py packages/nanolab/tests/test_provisioning.py -q`

Expected: PASS.

**Step 4: Commit.**

```bash
git add packages/nanolab/src/nanolab/cli/product.py packages/nanolab/src/nanolab/plans/cli.py packages/nanolab/tests/cli/test_release_command.py packages/nanolab/tests/plans/test_cli.py packages/nanolab/tests/test_provisioning.py
git commit -m "refactor: let providers own VM provisioning"
```

## Task 3: Fail closed when Azure existence cannot be determined

**Files:**

- Modify: `packages/sonata-tasks/src/sonata_tasks/vm/azure.py:95-135`
- Modify: `packages/sonata-tasks/tests/vm/test_azure_provider.py`

**Step 1: Write failing tests for Azure CLI outcomes.**

Keep a `ResourceNotFound` result as the one absence case. Add parameterized tests for expired credentials and network errors (non-zero return with non-not-found stderr) asserting both `ensure_running()` and `teardown()` raise a `RuntimeError` containing the Azure CLI error; neither `launch()` nor a successful teardown result is allowed.

**Step 2: Replace the boolean probe with a three-outcome probe.**

Implement a small private `_azure_vm_exists()` that returns `True` on exit 0, `False` only for Azure's `ResourceNotFound` response, and raises otherwise. Use it in `ensure_running()` and `teardown()`. Do not optimize away this cloud-authority check.

**Step 3: Verify.**

Run: `uv run pytest packages/sonata-tasks/tests/vm/test_azure_provider.py -q`

Expected: PASS.

**Step 4: Commit.**

```bash
git add packages/sonata-tasks/src/sonata_tasks/vm/azure.py packages/sonata-tasks/tests/vm/test_azure_provider.py
git commit -m "fix: fail closed on Azure existence probe errors"
```

## Task 4: Apply restricted Azure ingress after generic provisioning

**Files:**

- Modify: `packages/nanolab/src/nanolab/cli/provisioning.py:201-244`
- Modify: `packages/nanolab/src/nanolab/release/environment.py:174-208`
- Modify: `packages/nanolab/tests/test_provisioning.py`
- Modify: `packages/nanolab/tests/release/test_environment.py`

**Step 1: Write the failing generic-path test.**

For Azure + `operator_source_cidr` + a k8s/loadtest stack, assert the post-ensure hook invokes the existing Azure NSG rule operation for ports `30080`, `30081`, and `30090`, with the operator CIDR and the load-generator address where applicable. Assert `azure_open_ports is None` remains true: no temporary `0.0.0.0/0` rule is permitted.

**Step 2: Extract the reusable endpoint operation, not a second policy.**

Add a narrowly named helper in `nanolab.cli.provisioning` accepting `(environment, provider, stack_request, loadgen_request)`. It computes the operator CIDR plus the load-generator `/32` or `/128`, then calls `provider.restrict_inbound_sources(...)`. This keeps generic provisioning independent from the release package. Leave release validation in `release.environment`; have `secure_release_endpoints()` call the new helper and retain its URL-returning behavior. Call the helper from generic provisioning only after the relevant VM facts are available.

**Step 3: Verify.**

Run: `uv run pytest packages/nanolab/tests/test_provisioning.py packages/nanolab/tests/release/test_environment.py packages/nanolab/tests/cli/test_vm_provider.py -q`

Expected: PASS.

**Step 4: Commit.**

```bash
git add packages/nanolab/src/nanolab/cli/provisioning.py packages/nanolab/src/nanolab/release/environment.py packages/nanolab/tests/test_provisioning.py packages/nanolab/tests/release/test_environment.py packages/nanolab/tests/cli/test_vm_provider.py
git commit -m "fix: secure Azure NodePorts after generic provisioning"
```

## Task 5: Ensure transient release resources never survive `--keep`

**Files:**

- Modify: `packages/sonata-tasks/src/sonata_tasks/registry_tunnel.py:75-84`
- Modify: `packages/sonata-tasks/src/sonata_tasks/buildx.py:75-81`
- Modify: `packages/sonata-tasks/src/sonata_tasks/archive.py:116-121`
- Modify: `packages/sonata-tasks/tests/test_registry_tunnel.py`
- Modify: `packages/sonata-tasks/tests/test_buildx.py`
- Modify: `packages/sonata-tasks/tests/test_archive.py`

**Step 1: Write one assertion per resource.**

Each constructor test must assert `resource.always_release is True`; for the tunnel, retain the behavioral release-stop test.

**Step 2: Add `always_release=True` to the three returned `Resource` objects.**

Do not alter their acquire/release implementation or add teardown reconstruction: they are in-VM temporary state and must be released before the VM is retained.

**Step 3: Verify and commit.**

Run: `uv run pytest packages/sonata-tasks/tests/test_registry_tunnel.py packages/sonata-tasks/tests/test_buildx.py packages/sonata-tasks/tests/test_archive.py -q`

```bash
git add packages/sonata-tasks/src/sonata_tasks/{registry_tunnel,buildx,archive}.py packages/sonata-tasks/tests/test_{registry_tunnel,buildx,archive}.py
git commit -m "fix: always release transient release resources"
```

## Task 6: Make fresh release rotation safe and retained state reachable

**Files:**

- Modify: `packages/nanolab/src/nanolab/cli/product.py:189-207,470-540`
- Modify: `packages/nanolab/tests/cli/test_release_command.py`

**Step 1: Write failing tests.**

Add a test that holds `release_run_lock()` and proves a fresh run neither renames nor creates a journal. Add a retained-journal fixture and assert a fresh run refuses with a message containing both `--resume` and `--teardown`; its directory remains in place. Keep the existing non-retained supersede test.

**Step 2: Lock before inspection and rotation.**

Move the fresh-run journal check and `_supersede_release_run()` into the existing `with release_run_lock(...)` block, before workflow execution. Read the current journal's retained entries using the Sonata journal API; if any are held, raise `typer.BadParameter` rather than moving it. Do not scan or destroy `*.superseded-*`: automatic cleanup is unsafe.

**Step 3: Verify and commit.**

Run: `uv run pytest packages/nanolab/tests/cli/test_release_command.py -q`

```bash
git add packages/nanolab/src/nanolab/cli/product.py packages/nanolab/tests/cli/test_release_command.py
git commit -m "fix: protect retained release journals from superseding"
```

## Task 7: Apply the two one-line hygiene fixes

**Files:**

- Modify: `packages/nanolab/src/nanolab/plans/release.py:506-525,749-757`
- Modify: `packages/nanolab/tests/plans/test_release.py`
- Modify: `README.md:6`

**Step 1: Write focused regression checks.**

In the ARM workflow topology test, inspect the ARM runtime plan passed to the ARM task and assert its `image_plan is arm_plan`. In the attestation test, monkeypatch `verified_file_receipt` and assert the aggregate receipt is read once while constructing the predicate.

**Step 2: Make the minimal changes.**

Set `Amd64ReleasePlan.image_plan=arm_plan`; delete the discarded `release_record()` call at the top of `attest_images`; change the README package list to `packages/sonata-tasks`.

**Step 3: Verify and commit.**

Run: `uv run pytest packages/nanolab/tests/plans/test_release.py -q`

```bash
git add packages/nanolab/src/nanolab/plans/release.py packages/nanolab/tests/plans/test_release.py README.md
git commit -m "fix: keep ARM release plan and attestation inputs consistent"
```

## Task 8: Full verification

**Files:** none.

**Step 1: Run the workspace test suite.**

Run: `uv run pytest packages/nanolab/tests packages/sonata-tasks/tests -q`

Expected: PASS.

**Step 2: Run static checks and inspect the final diff.**

Run: `uv run ruff check packages/nanolab/src packages/nanolab/tests packages/sonata-tasks/src packages/sonata-tasks/tests`

Run: `uv run pyright packages/nanolab/src packages/sonata-tasks/src`

Run: `git diff --check`

Expected: all commands exit 0.

**Step 3: Commit the plan only if it was intentionally changed during implementation.**

```bash
git status --short
```

Do not add unrelated existing untracked files under `docs/superpowers/` or `release-v0.18.2-resume.log`.
