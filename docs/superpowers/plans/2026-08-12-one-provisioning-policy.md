# One Provisioning Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the decision "which bootstrap steps does this VM need, in which order" out of nanolab's three divergent copies and into one ordered table in `sonata_tasks`, leaving nanolab to say only *what a VM must be able to do*.

**Architecture:** A new `sonata_tasks/provisioning/policy.py` owns a `VmCapability` enum and a single ordered capability→planner table. Callers name a set of capabilities; the module decides which planners run and in what order, and returns validated `RemoteCommandOperation`s. The three existing call sites each become a capability set. No behaviour changes: every call site must produce the same operations, with the same argv, in the same order, as it does today.

**Tech Stack:** Python 3.12, uv workspace, pytest, basedpyright, ruff, import-linter.

## Global Constraints

- `sonata_tasks` must not import `nanolab` or `tui_toolkit`. Four import-linter contracts enforce this; `lint-imports` must stay at "0 broken" for every package.
- `basedpyright --project packages/<pkg>` must report `0 errors, 0 warnings, 0 notes` for `nanolab`, `sonata-tasks` and `tui-toolkit`.
- The sonata-tasks suite enforces a 90% coverage floor. New code in `policy.py` must be covered by tests, not by luck.
- `ruff check packages` must pass.
- **Behaviour must not change.** For every existing call site the produced operations must be identical in count, order, `operation_id`, `argv` and `env`. Every task below states how that is proven.
- Run the nanolab suite with `NANOFAAS_ROOT` pointing at a nanoFaaS checkout whose **git tree is clean**. A dirty tree fails `test_build_release_request_requires_credentials_for_execution` for an unrelated reason ("release requires a clean nanoFaaS Git tree"), which is not caused by this work.
- Commands are run from the repository root as
  `uv run --locked --all-packages --all-groups <tool>`.

---

## The three encodings being merged

Read these before starting. They are the specification: the new table must
reproduce all three exactly.

**1. `packages/nanolab/src/nanolab/cli/provisioning.py:45-69` (`_stack_operations`)**
— `plan_vm_provision_base`; then if `backend == "k8s"` or `workflow in ("loadtest", "release")`: `plan_k3s_install`, `plan_registry_ensure_container`, `plan_k3s_configure_registry`; then if `(workflow == "loadtest" and not dedicated_loadgen)` or `(workflow == "validate" and backend == "k8s")`: `plan_loadtest_install_k6`, `plan_assets_sync_to_vm`; then if `include_repo_sync`: `plan_repo_sync_to_vm`. All planners called with default keywords.
Its sibling roles, same file: loadgen (lines 146-168) is `plan_loadtest_install_k6`, `plan_assets_sync_to_vm`, and `plan_repo_sync_to_vm` unless `workflow == "release"`; cloud (170-186) is `_stack_operations` with `dedicated_loadgen=True`; arm-builder (188-197) is `plan_vm_provision_base` alone.

**2. `packages/nanolab/src/nanolab/plans/cli.py:72-78` (`_BOOTSTRAP_STEPS`)**
— `plan_vm_provision_base`, `plan_k3s_install`, `plan_repo_sync_to_vm`, each called with `discover_private_key=False`. It deliberately omits the registry steps: this workflow pulls published images from GHCR, so a local registry would be set up and never read.

**3. `packages/nanolab/src/nanolab/release/resources.py:205-228` (`_bootstrap_role`)**
— stack: `plan_vm_provision_base(install_uv=True)`, `plan_k3s_install`, `plan_registry_ensure_container`, `plan_k3s_configure_registry`; loadgen: `plan_loadtest_install_k6`, `plan_assets_sync_to_vm`; any other role: `plan_vm_provision_base(install_uv=True)`. No repository sync — release stages its source tree separately.

The three orders are mutually consistent, which is what makes one ordered
table possible: base → k3s → registry → configure-registry → k6 → assets →
repo. Verify this claim yourself against the three sites before Task 1; if any
site's order disagrees, stop and report it rather than "fixing" the order.

## File Structure

- **Create** `packages/sonata-tasks/src/sonata_tasks/provisioning/policy.py` — the `VmCapability` enum, the ordered capability→planner table, and `bootstrap_operations()`. Owns ordering and planner keywords. Owns no knowledge of workflows, backends or scenarios.
- **Create** `packages/sonata-tasks/tests/provisioning/test_policy.py` — tests for the table.
- **Modify** `packages/sonata-tasks/src/sonata_tasks/provisioning/__init__.py` — export the two new names.
- **Modify** `packages/nanolab/src/nanolab/release/resources.py` — `_bootstrap_role` names capabilities.
- **Modify** `packages/nanolab/src/nanolab/plans/cli.py` — `_BOOTSTRAP_STEPS` becomes a capability set.
- **Modify** `packages/nanolab/src/nanolab/cli/provisioning.py` — `_stack_operations` and its three sibling roles name capabilities.

---

### Task 1: The capability table

**Files:**
- Create: `packages/sonata-tasks/src/sonata_tasks/provisioning/policy.py`
- Create: `packages/sonata-tasks/tests/provisioning/test_policy.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/provisioning/__init__.py`

**Interfaces:**
- Consumes: `sonata_tasks.components.bootstrap` planners, `ScenarioExecutionContext`, `sonata_tasks.provisioning.bootstrap.remote_operations`.
- Produces: `VmCapability` (enum: `BASE`, `K3S`, `LOCAL_REGISTRY`, `K6`, `ASSETS`, `REPO`) and
  `bootstrap_operations(context: ScenarioExecutionContext, capabilities: Iterable[VmCapability], *, install_uv: bool = False, discover_private_key: bool = True) -> tuple[RemoteCommandOperation, ...]`.
  Tasks 2-4 call exactly this signature.

- [ ] **Step 1: Write the failing tests**

Create `packages/sonata-tasks/tests/provisioning/test_policy.py`, beside the
existing `test_bootstrap.py`, `test_environment.py`, `test_providers.py` and
`test_resources.py`. The directory already has an `__init__.py`; match the
sibling files' import style.

```python
from __future__ import annotations

from pathlib import Path

import pytest

from sonata_tasks.components.context import ScenarioExecutionContext
from sonata_tasks.provisioning.policy import VmCapability, bootstrap_operations
from sonata_tasks.vm.models import VmRequest


def _context(tmp_path: Path) -> ScenarioExecutionContext:
    return ScenarioExecutionContext(
        repo_root=tmp_path,
        scenario_name="policy-test",
        runtime="java",
        namespace=None,
        local_registry="localhost:5000",
        resolved_scenario=None,
        vm_request=VmRequest(
            lifecycle="external", name="vm", host="vm.example", user="ubuntu"
        ),
        cleanup_vm=False,
        assets_root=tmp_path / "assets",
    )


def test_capabilities_run_in_table_order_not_argument_order(tmp_path: Path) -> None:
    operations = bootstrap_operations(
        _context(tmp_path),
        [VmCapability.REPO, VmCapability.BASE, VmCapability.K3S],
    )

    assert [operation.operation_id for operation in operations] == [
        "vm.provision_base",
        "k3s.install",
        "repo.sync_to_vm",
    ]


def test_local_registry_ensures_the_container_then_points_k3s_at_it(
    tmp_path: Path,
) -> None:
    operations = bootstrap_operations(
        _context(tmp_path), [VmCapability.LOCAL_REGISTRY]
    )

    assert [operation.operation_id for operation in operations] == [
        "registry.ensure_container",
        "k3s.configure_registry",
    ]


def test_no_capabilities_produce_no_operations(tmp_path: Path) -> None:
    assert bootstrap_operations(_context(tmp_path), []) == ()


def test_a_capability_asked_for_twice_still_runs_once(tmp_path: Path) -> None:
    operations = bootstrap_operations(
        _context(tmp_path), [VmCapability.BASE, VmCapability.BASE]
    )

    assert len(operations) == 1


def test_install_uv_reaches_the_base_playbook(tmp_path: Path) -> None:
    without = bootstrap_operations(_context(tmp_path), [VmCapability.BASE])
    with_uv = bootstrap_operations(
        _context(tmp_path), [VmCapability.BASE], install_uv=True
    )

    assert "install_uv=false" in " ".join(without[0].argv)
    assert "install_uv=true" in " ".join(with_uv[0].argv)


def test_private_key_discovery_can_be_switched_off(tmp_path: Path) -> None:
    discovered = bootstrap_operations(_context(tmp_path), [VmCapability.BASE])
    plain = bootstrap_operations(
        _context(tmp_path), [VmCapability.BASE], discover_private_key=False
    )

    assert "--private-key" in discovered[0].argv
    assert "--private-key" not in plain[0].argv


def test_k6_takes_no_private_key_keyword(tmp_path: Path) -> None:
    """`plan_loadtest_install_k6` accepts neither keyword; passing one is a TypeError."""
    operations = bootstrap_operations(
        _context(tmp_path),
        [VmCapability.K6],
        install_uv=True,
        discover_private_key=False,
    )

    assert [operation.operation_id for operation in operations] == [
        "loadtest.install_k6"
    ]
```

Two of these encode assumptions about the current planners rather than about
the new module: `test_install_uv_reaches_the_base_playbook` and
`test_private_key_discovery_can_be_switched_off` assert on argv produced by
`plan_vm_provision_base`. Run those two assertions against the planner
directly first (in a scratch REPL, not a committed test) and adjust the
expected substrings to whatever it really emits. Do not adjust them later to
match whatever the code does — that would turn the test into a mirror.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked --all-packages --all-groups pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/provisioning/test_policy.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'sonata_tasks.provisioning.policy'`.

- [ ] **Step 3: Write the module**

Create `packages/sonata-tasks/src/sonata_tasks/provisioning/policy.py`:

```python
"""Which bootstrap steps a VM needs, and in which order — decided once."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sonata_tasks.components.bootstrap import (
    plan_assets_sync_to_vm,
    plan_k3s_configure_registry,
    plan_k3s_install,
    plan_loadtest_install_k6,
    plan_registry_ensure_container,
    plan_repo_sync_to_vm,
    plan_vm_provision_base,
)
from sonata_tasks.components.context import ScenarioExecutionContext
from sonata_tasks.components.operations import RemoteCommandOperation, ScenarioOperation
from sonata_tasks.provisioning.bootstrap import remote_operations


class VmCapability(Enum):
    """Something a provisioned VM must be able to do.

    Callers name these. Which planner provides one, and when it runs relative
    to the others, is this module's business — that is the whole point of the
    indirection, and the reason three call sites no longer each hold an
    opinion about it.
    """

    BASE = "base"
    K3S = "k3s"
    LOCAL_REGISTRY = "local-registry"
    K6 = "k6"
    ASSETS = "assets"
    REPO = "repo"


_Planner = Callable[..., tuple[ScenarioOperation, ...]]


@dataclass(frozen=True, slots=True)
class _Step:
    planner: _Planner
    # `plan_loadtest_install_k6` runs no SSH of its own and accepts neither
    # keyword, so passing one would raise TypeError rather than be ignored.
    takes_private_key: bool = True
    takes_install_uv: bool = False


# Ordered. A capability's steps run after every capability above it: k3s has to
# exist before its registry is configured, and the repository is synced last so
# a failed dependency install does not leave a half-copied tree behind.
_STEPS: tuple[tuple[VmCapability, tuple[_Step, ...]], ...] = (
    (VmCapability.BASE, (_Step(plan_vm_provision_base, takes_install_uv=True),)),
    (VmCapability.K3S, (_Step(plan_k3s_install),)),
    (
        VmCapability.LOCAL_REGISTRY,
        (_Step(plan_registry_ensure_container), _Step(plan_k3s_configure_registry)),
    ),
    (VmCapability.K6, (_Step(plan_loadtest_install_k6, takes_private_key=False),)),
    (VmCapability.ASSETS, (_Step(plan_assets_sync_to_vm),)),
    (VmCapability.REPO, (_Step(plan_repo_sync_to_vm),)),
)


def bootstrap_operations(
    context: ScenarioExecutionContext,
    capabilities: Iterable[VmCapability],
    *,
    install_uv: bool = False,
    discover_private_key: bool = True,
) -> tuple[RemoteCommandOperation, ...]:
    """The remote commands that give a VM the capabilities asked of it.

    Capabilities are a set: naming one twice runs it once, and the order they
    arrive in is ignored in favour of `_STEPS`. A caller that needs a different
    order needs a different table, not a different argument.

    `install_uv` is a property of the base playbook rather than a capability of
    its own, because it is one operation either way: release runs nanolab
    inside its VMs and so needs a Python launcher there, which the lab's own
    VMs do not.
    """
    requested = set(capabilities)
    operations: list[ScenarioOperation] = []
    for capability, steps in _STEPS:
        if capability not in requested:
            continue
        for step in steps:
            keywords: dict[str, Any] = {}
            if step.takes_private_key:
                keywords["discover_private_key"] = discover_private_key
            if step.takes_install_uv:
                keywords["install_uv"] = install_uv
            operations.extend(step.planner(context, **keywords))
    return remote_operations(operations)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --locked --all-packages --all-groups pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/provisioning/test_policy.py -q`
Expected: 7 passed.

- [ ] **Step 5: Export the names**

In `packages/sonata-tasks/src/sonata_tasks/provisioning/__init__.py`, add the
import and both names to `__all__`, following the file's existing grouped
style:

```python
from sonata_tasks.provisioning.policy import VmCapability, bootstrap_operations
```

- [ ] **Step 6: Verify the package**

Run, and expect all four clean:
```bash
uv run --locked --all-packages --all-groups pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -q
uv run --locked --all-packages --all-groups basedpyright --project packages/sonata-tasks
uv run --locked --all-packages --all-groups lint-imports --config packages/sonata-tasks/.importlinter --no-cache
uv run --locked --all-packages --all-groups ruff check packages
```

- [ ] **Step 7: Commit**

```bash
git add packages/sonata-tasks
git commit -m "feat: one table decides which bootstrap steps a VM needs"
```

---

### Task 2: Release names its capabilities

The smallest and most explicit of the three call sites, so it goes first: its
three branches are already literal planner lists.

**Files:**
- Modify: `packages/nanolab/src/nanolab/release/resources.py:205-228`
- Test: `packages/nanolab/tests/release/test_resources.py` — the only module that reaches `_bootstrap_role`

**Interfaces:**
- Consumes: `VmCapability`, `bootstrap_operations` from Task 1.
- Produces: nothing new. `_bootstrap_role` keeps its signature exactly.

- [ ] **Step 1: Pin the current behaviour before touching it**

Write a characterization test that records what `_bootstrap_role` produces
today, for all three role branches. It must assert on `operation_id`s in
order, because that is what must not change:

```python
def test_release_bootstrap_gives_each_role_its_current_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[tuple[str, tuple[str, ...]]] = []

    def record(provider, operations, *, role):
        recorded.append((role, tuple(op.operation_id for op in operations)))

    monkeypatch.setattr(
        "nanolab.release.resources.run_bootstrap_operations", record
    )
    request = VmRequest(lifecycle="external", name="vm", host="vm.example", user="ubuntu")
    info = VmInfo(name="vm", host="vm.example", user="ubuntu", home="/home/ubuntu")

    for role in ("stack", "loadgen", "arm-builder"):
        _bootstrap_role(object(), tmp_path, role, request, info)

    assert recorded == [
        (
            "stack",
            (
                "vm.provision_base",
                "k3s.install",
                "registry.ensure_container",
                "k3s.configure_registry",
            ),
        ),
        ("loadgen", ("loadtest.install_k6", "assets.sync_to_vm")),
        ("arm-builder", ("vm.provision_base",)),
    ]
```

Import `_bootstrap_role`, `VmRequest` and `VmInfo` as the surrounding test
module does. `retarget_cloud_operations` is called with `object()` as the
provider, which is neither an Azure nor a Proxmox provider — confirm from
`provisioning/bootstrap.py:135` that this path is identity for an external
request before relying on it; if it is not, patch that helper too.

- [ ] **Step 2: Run it to verify it passes against today's code**

Run: `NANOFAAS_ROOT=<clean checkout> uv run --locked --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml <the test file> -q`
Expected: PASS. A characterization test that fails now is describing something
other than the current behaviour — fix the test, not the code.

- [ ] **Step 3: Replace the branches**

In `packages/nanolab/src/nanolab/release/resources.py`, replace the body of
`_bootstrap_role` from `if role == "stack":` through the `raw = ...` branches
with:

```python
    if role == "stack":
        capabilities = (VmCapability.BASE, VmCapability.K3S, VmCapability.LOCAL_REGISTRY)
    elif role == "loadgen":
        capabilities = (VmCapability.K6, VmCapability.ASSETS)
    else:
        capabilities = (VmCapability.BASE,)
    # Release runs nanolab inside its VMs, so every one of them gets uv.
    operations = bootstrap_operations(context, capabilities, install_uv=True)
```

then keep the two existing lines that follow, with `remote_operations(raw)`
replaced by `operations`:

```python
    operations = retarget_cloud_operations(provider, context, operations)
    run_bootstrap_operations(provider, operations, role=role)
```

- [ ] **Step 4: Fix the imports**

Remove the six now-unused `plan_*` imports and `remote_operations` from the
file's import block — but only those that nothing else in the file uses. Run
`uv run --locked --all-packages --all-groups ruff check packages --fix` and
read what it removed rather than deleting by eye. Add:

```python
from sonata_tasks.provisioning.policy import VmCapability, bootstrap_operations
```

- [ ] **Step 5: Verify nothing moved**

Run: `NANOFAAS_ROOT=<clean checkout> uv run --locked --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q`
Expected: the characterization test from Step 1 still passes, unchanged, and
the rest of the suite is as green as it was before this task (one known
failure only if the checkout is dirty — see Global Constraints).

- [ ] **Step 6: Commit**

```bash
git add packages/nanolab
git commit -m "refactor: release names the capabilities its VMs need"
```

---

### Task 3: The provisioned CLI workflow names its capabilities

This one is the delicate one: its operations become Sonata task **titles**, and
those titles' slugs are asserted by an existing topology test.

**Files:**
- Modify: `packages/nanolab/src/nanolab/plans/cli.py:63-78` (`_BOOTSTRAP_STEPS`) and `:123-176` (`_bootstrap_tasks`)
- Test: `packages/nanolab/tests/plans/test_cli.py` (`test_provisioned_k8s_plan_compiles_the_expected_12_task_topology`, `test_provisioned_k8s_bootstrap_sets_up_no_local_registry`, `test_provisioned_k8s_plan_compilation_does_not_discover_ssh_credentials`, `test_provisioned_k8s_bootstrap_argv_is_resolved_from_the_acquired_vm` are the four that guard this)

**Interfaces:**
- Consumes: `VmCapability`, `bootstrap_operations` from Task 1.
- Produces: nothing new.

- [ ] **Step 1: Confirm the titles are the planners' own summaries**

`_BOOTSTRAP_STEPS` pairs each planner with a title and its comment claims the
titles "are not always the planner's own `summary`". Check that claim:

```bash
uv run --locked --all-packages --all-groups python -c "
from sonata_tasks.components.bootstrap import plan_vm_provision_base, plan_k3s_install, plan_repo_sync_to_vm
print([p.__name__ for p in (plan_vm_provision_base, plan_k3s_install, plan_repo_sync_to_vm)])
"
```
then read the three `summary=` literals in `components/bootstrap.py` and
compare them to the three titles in `_BOOTSTRAP_STEPS`.

Expected: they are identical — "Provision base VM dependencies", "Install
k3s", "Sync repository into VM" — and the comment is stale. **If they are not
identical, stop and report it**: the rest of this task assumes the title can
be read off the operation, and a mismatch means the compiled topology would
change.

- [ ] **Step 2: Rewrite the step list as a capability set**

Replace `_BOOTSTRAP_STEPS` (and its comment) with:

```python
# What a CLI-provisioned VM must be able to do. Deliberately no local
# registry: this workflow pulls published images from GHCR, so a registry
# would be set up and never read. `sonata_tasks.provisioning.policy` decides
# which steps that implies and in which order.
_BOOTSTRAP_CAPABILITIES = (VmCapability.BASE, VmCapability.K3S, VmCapability.REPO)
```

- [ ] **Step 3: Rewrite the loop over it**

In `_bootstrap_tasks`, replace the `for title, planner in _BOOTSTRAP_STEPS:`
loop header and the two lines that call the planner and type-check its result:

```python
    for base_operation in bootstrap_operations(
        placeholder_context, _BOOTSTRAP_CAPABILITIES, discover_private_key=False
    ):
```

`bootstrap_operations` returns `RemoteCommandOperation`s and raises on
anything else, so the `isinstance` check and the `TypeError` it raised are now
dead — delete both. Inside the loop, the task's title comes from the
operation:

```python
        tasks.append(
            CommandTask(
                title=base_operation.summary,
                argv=resolve_argv,
                executor=executor,
                role="host",
                env=base_operation.env,
            )
        )
```

Keep the `resolve_argv` closure exactly as it is, including its
`base_operation` default-argument binding — that binding is what stops every
task from closing over the last loop variable, and removing it would make all
three tasks run the same command.

Update `_bootstrap_tasks`'s docstring: it says "The 5 provisioning steps" and
there are three.

- [ ] **Step 4: Fix the imports**

Remove `plan_k3s_install`, `plan_repo_sync_to_vm`, `plan_vm_provision_base`
and, if nothing else in the file uses them, `ScenarioOperation` and
`Callable`. Let `ruff check packages --fix` decide. Add the policy import.

- [ ] **Step 5: Verify the compiled topology is byte-identical**

Run: `NANOFAAS_ROOT=<clean checkout> uv run --locked --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/plans/test_cli.py -q`
Expected: all pass, with **no edits to the test file**. If
`test_provisioned_k8s_plan_compiles_the_expected_12_task_topology` fails, the
titles changed and Step 1's assumption was wrong — revert and report, do not
update the expected topology.

- [ ] **Step 6: Commit**

```bash
git add packages/nanolab
git commit -m "refactor: the CLI workflow names the capabilities its VM needs"
```

---

### Task 4: Scenario provisioning names its capabilities

The largest of the three, and the only one whose capability set is computed
from configuration rather than written down.

**Files:**
- Modify: `packages/nanolab/src/nanolab/cli/provisioning.py:45-69` (`_stack_operations`) and `:126-199` (the four role blocks in `_role_requests_and_operations`)
- Test: `packages/nanolab/tests/test_provisioning.py`

**Interfaces:**
- Consumes: `VmCapability`, `bootstrap_operations` from Task 1.
- Produces: `_stack_capabilities(scenario, *, dedicated_loadgen, include_repo_sync) -> tuple[VmCapability, ...]` replacing `_stack_operations`. Private to this module.

- [ ] **Step 1: Find out which branch no test reaches**

`packages/nanolab/tests/test_provisioning.py` is already thorough. Its
`_playbooks(orchestrator)` helper returns the ordered playbook basenames, and
five tests assert on it or on `_commands(orchestrator)`:
`test_multipass_k8s_provisioning_composes_lifecycle_and_bootstrap_tasks`
(validate + k8s: the full six-step list),
`test_loadtest_provisions_dedicated_load_generator_with_k6`,
`test_offload_loadtest_syncs_repository_to_cloud`,
`test_arm_builder_role_is_ensured_torn_down_and_base_provisioned`, and
`test_external_provisioning_reuses_ssh_host_without_teardown`. Read all five.

Do not assume which branch of the condition at lines 63-66 is uncovered —
measure it:

```bash
NANOFAAS_ROOT=<clean checkout> uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/test_provisioning.py \
  -q --cov=nanolab.cli.provisioning --cov-branch --cov-report=term-missing
```

At the time of writing this reports `65->67, 106, 211` as missing. Line 106 is
the `retarget` early return for real cloud providers and 211 is the `local`
guard; neither is `_stack_operations`. The `65->67` arc is the false path of
the k6/assets condition — and reading the five tests suggests
`test_loadtest_provisions_dedicated_load_generator_with_k6` does exercise it,
so either the arc means something other than the obvious or a test is not
reaching the code it appears to. **Resolve that contradiction before
changing anything**, because the whole safety argument for this task rests on
knowing which branches are actually pinned.

For whichever branch turns out to be genuinely unreached, add a
characterization test in the shape of the existing ones — construct a
`RecordingOrchestrator`, pass it via `orchestrator_factory=lambda _: orchestrator`,
and assert on `_playbooks(orchestrator)`, e.g.:

```python
    assert _playbooks(orchestrator) == [
        "provision-base.yml",
        "provision-k3s.yml",
        "ensure-registry.yml",
        "configure-k3s-registry.yml",
        "install-k6.yml",
    ]
```

The requirement is that the test fails if that branch's steps change.

- [ ] **Step 2: Run the five existing tests plus the new one**

Run: `NANOFAAS_ROOT=<clean checkout> uv run --locked --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/test_provisioning.py -q`
Expected: all pass against today's code.

- [ ] **Step 3: Replace `_stack_operations` with `_stack_capabilities`**

```python
def _stack_capabilities(
    scenario: ScenarioConfig,
    *,
    dedicated_loadgen: bool,
    include_repo_sync: bool = True,
) -> tuple[VmCapability, ...]:
    """What a stack VM must be able to do for this scenario.

    Reading the scenario is nanolab's half of the split: which steps a
    capability implies, and their order, belong to
    `sonata_tasks.provisioning.policy`.
    """
    capabilities = [VmCapability.BASE]
    if scenario.backend == "k8s" or scenario.workflow in ("loadtest", "release"):
        capabilities.extend([VmCapability.K3S, VmCapability.LOCAL_REGISTRY])
    if (scenario.workflow == "loadtest" and not dedicated_loadgen) or (
        scenario.workflow == "validate" and scenario.backend == "k8s"
    ):
        capabilities.extend([VmCapability.K6, VmCapability.ASSETS])
    if include_repo_sync:
        capabilities.append(VmCapability.REPO)
    return tuple(capabilities)
```

Note it no longer takes `context`: it decides *what*, not *how*.

- [ ] **Step 4: Rewrite the four role blocks**

Stack (currently lines 130-144) — the `retarget(...)` wrapper and the
`triples.append` stay; only the operations argument changes:

```python
            retarget(
                stack_context,
                bootstrap_operations(
                    stack_context,
                    _stack_capabilities(
                        scenario,
                        dedicated_loadgen=dedicated_loadgen,
                        include_repo_sync=scenario.workflow != "release",
                    ),
                ),
            ),
```

Loadgen (currently 146-168):

```python
        loadgen_capabilities = [VmCapability.K6, VmCapability.ASSETS]
        if scenario.workflow != "release":
            loadgen_capabilities.append(VmCapability.REPO)
        triples.append(
            (
                "loadgen",
                loadgen_request,
                retarget(
                    loadgen_context,
                    bootstrap_operations(loadgen_context, loadgen_capabilities),
                ),
            )
        )
```

Cloud (currently 170-186) — same shape as stack, with
`dedicated_loadgen=True` and the default `include_repo_sync`:

```python
                retarget(
                    cloud_context,
                    bootstrap_operations(
                        cloud_context,
                        _stack_capabilities(scenario, dedicated_loadgen=True),
                    ),
                ),
```

arm-builder (currently 188-197):

```python
                bootstrap_operations(arm_context, (VmCapability.BASE,)),
```

- [ ] **Step 5: Fix the imports**

The seven `plan_*` names and `remote_operations` should now be unused in this
file; `retarget_bootstrap_operation` and `ScenarioOperation` may not be. Run
`ruff check packages --fix` and read what it removed. Add the policy import.

- [ ] **Step 6: Verify**

Run:
```bash
NANOFAAS_ROOT=<clean checkout> uv run --locked --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
uv run --locked --all-packages --all-groups basedpyright --project packages/nanolab
uv run --locked --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
```
Expected: the six tests from Step 1-2 pass **with no edits**, the type check is
clean, contracts kept.

- [ ] **Step 7: Commit**

```bash
git add packages/nanolab
git commit -m "refactor: scenario provisioning names the capabilities its VMs need"
```

---

### Task 5: Prove there is one encoding left

**Files:**
- Test: `packages/sonata-tasks/tests/provisioning/test_policy.py`
- Modify: whichever of the three call sites still carries a stale comment

- [ ] **Step 1: Search for a fourth copy**

```bash
grep -rn "plan_k3s_install\|plan_vm_provision_base\|plan_registry_ensure_container" \
  --include="*.py" packages/*/src
```
Expected: hits only in `components/bootstrap.py` (the definitions) and
`provisioning/policy.py` (the table). Any other source hit is a call site this
plan missed — report it rather than silently converting it.

- [ ] **Step 2: Add the regression guard**

Append to `test_policy.py`:

```python
def test_the_planners_have_exactly_one_caller(tmp_path: Path) -> None:
    """The point of the table: nothing outside it decides what a VM installs.

    A new call site that imports a planner directly re-opens the split this
    module closed, and would not otherwise fail any test.
    """
    del tmp_path
    root = Path(__file__).resolve().parents[4]
    offenders = sorted(
        path.relative_to(root)
        for path in root.glob("packages/*/src/**/*.py")
        if "plan_k3s_install" in path.read_text(encoding="utf-8")
        and path.name not in {"bootstrap.py", "policy.py"}
    )

    assert offenders == []
```

Verify `parents[4]` actually lands on the repository root from that test
file's location; adjust the index if it does not, and assert on something at
the root (e.g. `(root / "pyproject.toml").is_file()`) rather than trusting the
count.

- [ ] **Step 3: Run it**

Run: `uv run --locked --all-packages --all-groups pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/provisioning/test_policy.py -q`
Expected: 8 passed.

- [ ] **Step 4: Sweep the stale comments**

Three comments described the duplication and are now wrong:
- `plans/cli.py` — the "reuses from the legacy provisioning components" block, if any of it survived Task 3.
- `cli/provisioning.py` — the module docstring still says "translate nanolab config onto sonata_tasks provisioning resources", which is now precisely true; leave it.
- `plans/cli.py:53-58` — the `_LOCAL_REGISTRY` comment says "the two bootstrap planners that consumed this value are not reused". Still true, but it now reads oddly next to a capability set; reword it to name `VmCapability.LOCAL_REGISTRY` as the thing not requested.

- [ ] **Step 5: Full gate**

Run every command in the README's CI-gate block against a clean nanoFaaS
checkout. Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add packages docs
git commit -m "test: guard the single provisioning policy against a fourth copy"
```

---

## What this plan deliberately does not do

**It does not replace `provision_environment` with Sonata resources.** That is
the larger half of the debt and it is a separate plan, for two reasons.

The first is technical. `provision_environment` is a `@contextmanager`: no
journal, no compensation, invisible to `--only`. Converting it means the four
workflows it serves — validate, cli-container, loadtest, offload-loadtest —
change how they acquire and release VMs, and it means changing
`nanolab/tui/app.py:409`, where the TUI wraps a whole workflow run in it. That
is a behaviour change in the run loop, not a refactor.

The second is that **there is currently no end-to-end run to check it
against.** The multipass VM is gone, and no scenario has been run start to
finish since the workflow-findings work landed. A migration of the VM
lifecycle whose only evidence is unit tests is a migration that will be
debugged in production. The entry criterion for that plan is a green
`autoscaling-cycle-k8s-hpa.yaml` run recorded before the work starts, so
there is a known-good baseline to diff against.

**It does not merge the two "plan before the VM exists" mechanisms.**
`cli/provisioning.py` fabricates a resolved-equivalent request
(`model_copy(lifecycle="external", host=f"{name}.internal")`) and retargets
eagerly; `plans/cli.py` retargets lazily at run time from the real `VmInfo`.
The lazy one is correct — it uses the host that exists rather than one it
invented — but the eager one is load-bearing for the cloud providers, whose
endpoint helpers cannot run before ensure. Collapsing them belongs with the
resources migration, because that is what removes the need to plan early at
all.

**It does not touch `build_role_bindings`.** Its provider ladder in
`cli/execution.py:319-331` looks like the one that was just unified and is
not: there `external` takes a plain SSH runner and constructs no VM provider.

## One risk worth naming

Every task claims "no behaviour change" and proves it with tests written
against the current code. That proof is worth exactly as much as the coverage
of the branch being moved, and no more.

`cli/provisioning.py` measures at 95% statement and near-full branch coverage,
which sounds like enough and is the thing to distrust: Task 4 Step 1 exists
because one reported-uncovered arc could not be reconciled with a test that
plainly appears to exercise it. Resolve that before moving code. If, while
working, you find any other branch no test reaches, write the characterization
test first — that is not scope creep, it is the only thing that makes the
refactor safe.

Task 2's characterization test is the weakest of the three, because
`_bootstrap_role`'s three branches are currently exercised only through
`packages/nanolab/tests/release/test_resources.py`. Read what that file
already asserts before writing a new test: if it pins the playbook sequence
per role, the new test is redundant and should be dropped rather than
duplicated.
