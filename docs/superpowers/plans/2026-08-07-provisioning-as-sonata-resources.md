# Provisioning as Sonata Resources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace nanolab's legacy `cli/provisioning.py` + `cli/vm_provider.py` with a self-contained `sonata_tasks/provisioning/` package (provider factory, VM resources, bootstrap operations, environment composite), then delete the `workflow-tasks` package entirely.

**Architecture:** The provisioning resources are well-circumscribed: they consume only sonata types (`VmRequest`, `RemoteCommandOperation`, provider objects) and absorb the VM provider selection (multipass/azure/proxmox). nanolab keeps only config translation (`EnvironmentConfig`/`ScenarioConfig` → requests + operations) at the boundary — the import-linter contract forbids `sonata_tasks → nanolab`. The release path (`release/resources.py`) already implements this pattern with `vm_resource`; the new package is the extraction and generalization of that prototype, and `release/resources.py` re-points onto it. When nanolab's legacy provisioning path is gone, `workflow_tasks.core` has no consumers left and the package is deleted.

**Tech Stack:** Python 3.12, sonata-engine (Resources/Steps/compensation), pydantic, pytest (90% coverage gate per package), import-linter, uv workspace.

## Global Constraints

- `sonata_tasks` must not import `nanolab` or `tui_toolkit` (contract in `packages/sonata-tasks/.importlinter`).
- `sonata_tasks` must not import `workflow_tasks.core` (contract in `packages/sonata-tasks/.importlinter`).
- `sonata_tasks` may import `workflow_tasks` only for nothing — after Task 6 the package is gone; no new imports may be added.
- Coverage gate is 90% per package (`--cov-fail-under=90` in each pyproject).
- Run tests per package: `pytest -c packages/<pkg>/pyproject.toml packages/<pkg>/tests`.
- No hard-coded cell counts in tests; assert expansion rules.
- Behavior must be preserved: the 13 tests in `packages/nanolab/tests/test_provisioning.py` keep passing (possibly re-pointed, never weakened).

---

### Task 1: Provider factory in sonata_tasks

Move provider selection out of nanolab into a lifecycle-keyed factory. The factory absorbs `vm_provider_for_environment`'s branching, keyed on `VmRequest.lifecycle` (which equals the provider string) instead of `EnvironmentConfig.provider`.

**Files:**
- Create: `packages/sonata-tasks/src/sonata_tasks/provisioning/__init__.py`
- Create: `packages/sonata-tasks/src/sonata_tasks/provisioning/providers.py`
- Create: `packages/sonata-tasks/tests/provisioning/__init__.py`
- Create: `packages/sonata-tasks/tests/provisioning/test_providers.py`
- Modify: `packages/nanolab/src/nanolab/cli/vm_provider.py` (delegate the factory body)

**Interfaces:**
- Consumes: `sonata_tasks.vm.models.VmRequest` (exists), `sonata_tasks.vm.orchestrator.VmOrchestrator` (exists), `sonata_tasks.vm.azure.AzureVmProvider` (exists), `sonata_tasks.vm.proxmox.ProxmoxVmProvider` (exists).
- Produces: `sonata_tasks.provisioning.providers.provider_for(request: VmRequest, repo_root: Path) -> object` — raises `ValueError` for `local`/`external` lifecycles.

- [ ] **Step 1: Write the failing test**

Create `packages/sonata-tasks/tests/provisioning/test_providers.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from sonata_tasks.provisioning.providers import provider_for
from sonata_tasks.vm.azure import AzureVmProvider
from sonata_tasks.vm.models import VmRequest
from sonata_tasks.vm.orchestrator import VmOrchestrator
from sonata_tasks.vm.proxmox import ProxmoxVmProvider


def test_provider_for_multipass_returns_orchestrator(tmp_path: Path) -> None:
    provider = provider_for(VmRequest(lifecycle="multipass"), tmp_path)
    assert isinstance(provider, VmOrchestrator)


def test_provider_for_azure_returns_azure_provider(tmp_path: Path) -> None:
    provider = provider_for(VmRequest(lifecycle="azure"), tmp_path)
    assert isinstance(provider, AzureVmProvider)


def test_provider_for_proxmox_returns_proxmox_provider(tmp_path: Path) -> None:
    provider = provider_for(VmRequest(lifecycle="proxmox"), tmp_path)
    assert isinstance(provider, ProxmoxVmProvider)


def test_provider_for_external_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="external"):
        provider_for(VmRequest(lifecycle="external"), tmp_path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/provisioning/test_providers.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'sonata_tasks.provisioning'`

- [ ] **Step 3: Write the implementation**

Create `packages/sonata-tasks/src/sonata_tasks/provisioning/providers.py`:

```python
"""Provider selection: request lifecycle -> concrete managed VM provider."""

from __future__ import annotations

from pathlib import Path

from sonata_tasks.vm.azure import AzureVmProvider
from sonata_tasks.vm.models import VmRequest
from sonata_tasks.vm.orchestrator import VmOrchestrator
from sonata_tasks.vm.proxmox import ProxmoxVmProvider


def provider_for(request: VmRequest, repo_root: Path) -> object:
    """Return the managed VM provider for a request's lifecycle.

    `external` has no managed provider: the caller already owns the VM.
    """
    if request.lifecycle == "multipass":
        return VmOrchestrator(repo_root)
    if request.lifecycle == "azure":
        return AzureVmProvider(repo_root)
    if request.lifecycle == "proxmox":
        return ProxmoxVmProvider(repo_root)
    raise ValueError(f"{request.lifecycle} does not use a managed VM provider")
```

Create `packages/sonata-tasks/src/sonata_tasks/provisioning/__init__.py`:

```python
"""Self-contained provisioning: provider selection, VM resources, bootstrap."""

from sonata_tasks.provisioning.providers import provider_for

__all__ = ["provider_for"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/provisioning/test_providers.py -q`
Expected: PASS

- [ ] **Step 5: Delegate nanolab's factory to it**

Modify `packages/nanolab/src/nanolab/cli/vm_provider.py`:

```python
from pathlib import Path

from sonata_tasks.provisioning.providers import provider_for
from sonata_tasks.vm.models import VmRequest

from nanolab.config.environment import EnvironmentConfig, ExecutionRole
```

Replace the body of `vm_provider_for_environment` with:

```python
def vm_provider_for_environment(environment: EnvironmentConfig, repo_root: Path) -> object:
    return provider_for(VmRequest(lifecycle=environment.provider), repo_root)
```

(Remove the now-unused `AzureVmProvider`/`ProxmoxVmProvider` imports.)

- [ ] **Step 6: Run nanolab suite to verify no regression**

Run: `NANOFAAS_ROOT=$HOME/Downloads/mcFaas pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q`
Expected: PASS (the pre-existing `test_help_does_not_require_nanofaas_root` environment failure is allowed)

- [ ] **Step 7: Commit**

```bash
git add packages/sonata-tasks packages/nanolab/src/nanolab/cli/vm_provider.py
git commit -m "feat: lifecycle-keyed provider factory in sonata_tasks.provisioning"
```

---

### Task 2: Verified VM resource

Extract the release path's `_VerifiedLifecycle` + `_vm` pattern (`packages/nanolab/src/nanolab/release/resources.py:195-224`) into `sonata_tasks/provisioning/resources.py`, and re-point `release/resources.py` onto it.

**Files:**
- Create: `packages/sonata-tasks/src/sonata_tasks/provisioning/resources.py`
- Create: `packages/sonata-tasks/tests/provisioning/test_resources.py`
- Modify: `packages/nanolab/src/nanolab/release/resources.py` (delete local `_VerifiedLifecycle` and `_vm`; import `provisioned_vm`)

**Interfaces:**
- Consumes: `sonata_tasks.vm.vm_resource` (exists), `sonata_tasks.vm.adapters.VmLifecycleAdapter` (exists), `sonata_tasks.vm.models.{VmConfig, VmInfo, VmRequest, vm_remote_home}` (exist), `sonata_engine.Resource`.
- Produces: `provisioned_vm(*, title: str, request: VmRequest, provider: object, after_ensure: Callable[[VmInfo], None] | None = None, requires: tuple[Resource[Any], ...] = ()) -> Resource[VmInfo]` and `VerifiedLifecycle`.

- [ ] **Step 1: Write the failing test**

Create `packages/sonata-tasks/tests/provisioning/test_resources.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

from sonata_tasks.provisioning.resources import VerifiedLifecycle, provisioned_vm
from sonata_tasks.vm.models import VmConfig, VmInfo, VmRequest


@dataclass
class FakeProvider:
    ensured: list[VmConfig] = None  # type: ignore[assignment]
    destroyed: list[VmInfo] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.ensured: list[VmConfig] = []
        self.destroyed: list[VmInfo] = []

    def ensure_running(self, request: VmRequest) -> object:
        self.ensured.append(
            VmConfig(name=request.name or "x", cpus=request.cpus, memory=request.memory, disk=request.disk)
        )
        return _Result(return_code=0)

    def connection_host(self, request: VmRequest) -> str:
        return "10.0.0.5"

    def teardown(self, request: VmRequest) -> None:
        self.destroyed.append(VmInfo(name=request.name or "x", host="10.0.0.5", user="ubuntu", home="/home/ubuntu"))


@dataclass
class _Result:
    return_code: int


def test_verified_lifecycle_runs_verifier_after_ensure() -> None:
    calls: list[VmInfo] = []
    lifecycle = VerifiedLifecycle(
        VmLifecycleAdapter(FakeProvider(), lifecycle="multipass"),
        calls.append,
    )
    info = lifecycle.ensure_running(VmConfig(name="stack"))
    assert calls == [info]


def test_provisioned_vm_builds_resource_with_fallback_info() -> None:
    request = VmRequest(lifecycle="multipass", name="stack", host="10.0.0.5")
    resource = provisioned_vm(title="stack", request=request, provider=FakeProvider())
    assert resource.title == "stack"
```

(Add `from sonata_tasks.vm.adapters import VmLifecycleAdapter` to the test imports.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/provisioning/test_resources.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'sonata_tasks.provisioning.resources'`

- [ ] **Step 3: Write the implementation**

Create `packages/sonata-tasks/src/sonata_tasks/provisioning/resources.py`:

```python
"""VM resources for provisioning: acquire, verify, release per role."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from sonata_engine import Resource

from sonata_tasks.vm import vm_resource
from sonata_tasks.vm.adapters import VmLifecycleAdapter
from sonata_tasks.vm.models import VmConfig, VmInfo, VmRequest, vm_remote_home


class VerifiedLifecycle:
    """VmLifecycleProtocol wrapper that runs a verifier after each acquire."""

    def __init__(
        self,
        lifecycle: VmLifecycleAdapter,
        after_ensure: Callable[[VmInfo], None],
    ) -> None:
        self._lifecycle = lifecycle
        self._after_ensure = after_ensure

    def ensure_running(self, config: VmConfig) -> VmInfo:
        info = self._lifecycle.ensure_running(config)
        self._after_ensure(info)
        return info

    def destroy(self, info: VmInfo) -> None:
        self._lifecycle.destroy(info)


def provisioned_vm(
    *,
    title: str,
    request: VmRequest,
    provider: object,
    after_ensure: Callable[[VmInfo], None] | None = None,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[VmInfo]:
    """A Sonata VM resource that ensures, verifies, and compensates the VM."""
    config = VmConfig(
        name=request.name or request.host or title,
        cpus=request.cpus,
        memory=request.memory,
        disk=request.disk,
    )
    lifecycle = VerifiedLifecycle(
        VmLifecycleAdapter(provider, lifecycle=request.lifecycle, credentials=request),
        after_ensure or (lambda _info: None),
    )
    resource = vm_resource(
        title=title,
        lifecycle=lifecycle,  # type: ignore[arg-type]
        config=config,
        fallback_info=VmInfo(
            name=config.name,
            host=request.host or "",
            user=request.user,
            home=vm_remote_home(request),
        ),
    )
    return replace(resource, requires=requires)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/provisioning/test_resources.py -q`
Expected: PASS

- [ ] **Step 5: Re-point release/resources.py**

In `packages/nanolab/src/nanolab/release/resources.py`:
- Delete the local `_VerifiedLifecycle` class (lines ~195-203) and the local `_vm` function (lines ~207-224).
- Replace the import `from sonata_tasks.vm import vm_resource` with `from sonata_tasks.provisioning.resources import provisioned_vm`.
- Replace the two call sites `_vm(` with `provisioned_vm(` (they already pass `title=`/`request=`/`provider=`/`after_ensure=`/`requires=` keyword args — signatures match).
- Remove the now-unused imports `VmConfig`, `VmLifecycleAdapter` if no other use remains in the file (check with `grep -n "VmConfig\|VmLifecycleAdapter" packages/nanolab/src/nanolab/release/resources.py`).
- `replace` and `Resource` imports stay (used elsewhere in the file).

- [ ] **Step 6: Run release + sonata suites**

Run: `NANOFAAS_ROOT=$HOME/Downloads/mcFaas pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/plans/test_release.py packages/nanolab/tests/release -q`
Expected: PASS
Run: `pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -q`
Expected: PASS, coverage ≥ 90%

- [ ] **Step 7: Commit**

```bash
git add packages/sonata-tasks packages/nanolab/src/nanolab/release/resources.py
git commit -m "refactor: extract provisioned_vm resource from the release path"
```

---

### Task 3: Bootstrap operations in sonata_tasks

Move the operation-running helpers out of `nanolab/cli/provisioning.py` (`_run_operations`, `_remote_operations`, `_retarget_cloud_operations`, `_context`, and the `_OperationTask` dataclass) into `sonata_tasks/provisioning/bootstrap.py`, with the nanolab config types replaced by their sonata equivalents (`request.lifecycle` instead of `environment.provider`, explicit `assets_root` instead of `discover_tool_root()`).

**Files:**
- Create: `packages/sonata-tasks/src/sonata_tasks/provisioning/bootstrap.py`
- Create: `packages/sonata-tasks/tests/provisioning/test_bootstrap.py`
- Modify: `packages/nanolab/src/nanolab/release/resources.py` (import the new helpers)
- Modify: `packages/nanolab/src/nanolab/cli/provisioning.py` (re-point its internal helpers — the old ones are deleted in Task 5; here they delegate or the module imports from sonata)

**Interfaces:**
- Consumes: `sonata_tasks.components.context.ScenarioExecutionContext`, `sonata_tasks.components.operations.{RemoteCommandOperation, ScenarioOperation}`, `sonata_tasks.components.bootstrap.retarget_bootstrap_operation`, `sonata_tasks.shell.SubprocessShell`, `sonata_tasks.tasks.executors.HostCommandTaskExecutor`, `sonata_tasks.tasks.models.{CommandTaskSpec, TaskResult}`, `sonata_tasks.workflow.reporting.workflow_step`.
- Produces:
  - `scenario_context(repo_root: Path, request: VmRequest, assets_root: Path) -> ScenarioExecutionContext`
  - `remote_operations(operations: Iterable[ScenarioOperation]) -> tuple[RemoteCommandOperation, ...]`
  - `retarget_cloud_operations(orchestrator: object, context: ScenarioExecutionContext, operations: Iterable[RemoteCommandOperation]) -> tuple[RemoteCommandOperation, ...]`
  - `run_bootstrap_operations(provider: object, operations: Iterable[RemoteCommandOperation], *, role: str) -> None`

- [ ] **Step 1: Write the failing test**

Create `packages/sonata-tasks/tests/provisioning/test_bootstrap.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from sonata_tasks.components.context import ScenarioExecutionContext
from sonata_tasks.components.operations import RemoteCommandOperation
from sonata_tasks.provisioning.bootstrap import (
    remote_operations,
    retarget_cloud_operations,
    run_bootstrap_operations,
    scenario_context,
)
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult
from sonata_tasks.vm.models import VmRequest


@dataclass
class RecordingShell:
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, argv: list[str], *, cwd, env, dry_run: bool) -> TaskResult:
        self.seen.append(CommandTaskSpec(task_id="x", summary="x", argv=tuple(argv)))
        return TaskResult(task_id="x", status="passed", return_code=0)


@dataclass
class FakeOrchestrator:
    shell: RecordingShell = field(default_factory=RecordingShell)
    retargeted: list[str] = field(default_factory=list)

    def ssh_endpoint(self, request: VmRequest) -> tuple[str, int]:
        return "10.0.0.5", 22

    def ssh_private_key_path(self, request: VmRequest) -> str:
        return "/keys/id_rsa"


def test_scenario_context_carries_request() -> None:
    request = VmRequest(lifecycle="multipass", name="stack")
    context = scenario_context(Path("/repo"), request, Path("/assets"))
    assert context.vm_request == request
    assert context.assets_root == Path("/assets")


def test_remote_operations_filters_non_remote() -> None:
    op = RemoteCommandOperation(operation_id="a", summary="s", argv=("true",))
    assert remote_operations([op]) == (op,)


def test_run_bootstrap_operations_records_each_command() -> None:
    provider = FakeOrchestrator()
    op = RemoteCommandOperation(operation_id="k3s", summary="install", argv=("helm", "install"))
    run_bootstrap_operations(provider, [op], role="stack")
    assert provider.shell.seen[0].argv == ("helm", "install")


def test_retarget_cloud_operations_uses_ssh_endpoint() -> None:
    provider = FakeOrchestrator()
    context = scenario_context(Path("/repo"), VmRequest(lifecycle="proxmox", name="stack"), Path("/assets"))
    op = RemoteCommandOperation(
        operation_id="base",
        summary="base",
        argv=("ansible-playbook", "-i", "unused", "playbook.yml"),
    )
    retargeted = retarget_cloud_operations(provider, context, [op])
    assert "-e" in retargeted[0].argv
    assert "ansible_port=22" in retargeted[0].argv
```

(Add `from pathlib import Path` to the test imports.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/provisioning/test_bootstrap.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

Create `packages/sonata-tasks/src/sonata_tasks/provisioning/bootstrap.py` — transcribe from `packages/nanolab/src/nanolab/cli/provisioning.py` (lines 101-235) with these changes:

- `_context` → `scenario_context(repo_root, request, assets_root)`; add the `assets_root` parameter (nanolab passed `discover_tool_root() / "assets"`).
- `_OperationTask` → keep the same dataclass, renamed `OperationTask` (no legacy imports — it only uses sonata `CommandTaskSpec`/`TaskResult`).
- `_operation_task` → `operation_task` (same body).
- `_run_operations` → `run_bootstrap_operations(provider, operations, *, role)`; body: `runner = getattr(provider, "shell", None) or SubprocessShell()`; build `OperationTask`s with `replace(op, operation_id=f"provision.{role}.{op.operation_id}")`; run each inside `with workflow_step(task_id=task.task_id, title=task.title): task.run()`.
- `_remote_operations` → `remote_operations` (same body).
- `_retarget_cloud_operations(environment, orchestrator, context, operations)` → `retarget_cloud_operations(orchestrator, context, operations)`: replace `environment.provider not in {"azure", "proxmox"}` with `context.vm_request.lifecycle not in {"azure", "proxmox"}`, and `environment.provider == "proxmox"` with `context.vm_request.lifecycle == "proxmox"`.

Also add to `packages/sonata-tasks/src/sonata_tasks/provisioning/__init__.py`:

```python
from sonata_tasks.provisioning.bootstrap import (
    remote_operations,
    retarget_cloud_operations,
    run_bootstrap_operations,
    scenario_context,
)
from sonata_tasks.provisioning.providers import provider_for
from sonata_tasks.provisioning.resources import VerifiedLifecycle, provisioned_vm

__all__ = [
    "provider_for",
    "VerifiedLifecycle", "provisioned_vm",
    "scenario_context", "remote_operations",
    "retarget_cloud_operations", "run_bootstrap_operations",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/provisioning/test_bootstrap.py -q`
Expected: PASS

- [ ] **Step 5: Re-point release/resources.py**

In `packages/nanolab/src/nanolab/release/resources.py`, replace:

```python
from nanolab.cli.provisioning import (
    _context,
    _remote_operations,
    _retarget_cloud_operations,
    _run_operations,
)
```

with:

```python
from sonata_tasks.provisioning import (
    remote_operations,
    retarget_cloud_operations,
    run_bootstrap_operations,
    scenario_context,
)
```

Then update the call sites in the file: `_context(repo_root, resolved)` → `scenario_context(repo_root, resolved, discover_tool_root() / "assets")`; `_retarget_cloud_operations(environment, provider, context, ...)` → `retarget_cloud_operations(provider, context, ...)`; `_remote_operations(raw)` → `remote_operations(raw)`; `_run_operations(provider, operations, role=role)` → `run_bootstrap_operations(provider, operations, role=role)`. Add `from nanolab.workspace.paths import discover_tool_root` if not already imported.

- [ ] **Step 6: Run suites**

Run: `NANOFAAS_ROOT=$HOME/Downloads/mcFaas pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/plans/test_release.py packages/nanolab/tests/release -q` and `pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -q`
Expected: PASS both

- [ ] **Step 7: Commit**

```bash
git add packages/sonata-tasks packages/nanolab/src/nanolab/release/resources.py
git commit -m "refactor: bootstrap operations live in sonata_tasks.provisioning"
```

---

### Task 4: Environment composite in sonata_tasks

Build the multi-role composite (ensure all role VMs → run per-role bootstrap operations → tear down unless `keep`) in `sonata_tasks/provisioning/environment.py`, modeled on `provision_environment` from `packages/nanolab/src/nanolab/cli/provisioning.py` (lines 238-410). It consumes only sonata types; nanolab maps config onto it.

**Files:**
- Create: `packages/sonata-tasks/src/sonata_tasks/provisioning/environment.py`
- Create: `packages/sonata-tasks/tests/provisioning/test_environment.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/provisioning/__init__.py` (re-export)

**Interfaces:**
- Consumes: `provider_for`, `scenario_context`, `retarget_cloud_operations`, `run_bootstrap_operations`, `remote_operations`, `sonata_tasks.vm.tasks.DestroyVm`, `sonata_tasks.vm.adapters.VmLifecycleAdapter`, `sonata_tasks.vm.models.{VmInfo, vm_remote_home}`, `sonata_tasks.workflow.reporting.workflow_step`.
- Produces:
  - `@dataclass(frozen=True) ProvisionedRole: role: str; request: VmRequest; operations: tuple[RemoteCommandOperation, ...] = ()`
  - `provision_roles(provider: object, roles: tuple[ProvisionedRole, ...], *, repo_root: Path, assets_root: Path, keep: bool = False, after_ensure: Callable[[str, VmRequest], None] | None = None) -> AbstractContextManager[None]` — on exit, destroys each role's VM in reverse order unless `keep`; aggregates main/cleanup errors exactly like the legacy `provision_environment` does (raise the main error, or the cleanup error, or both combined).

- [ ] **Step 1: Write the failing test**

Create `packages/sonata-tasks/tests/provisioning/test_environment.py` — a minimal version of the legacy behavior:

```python
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from sonata_tasks.components.operations import RemoteCommandOperation
from sonata_tasks.provisioning.environment import ProvisionedRole, provision_roles
from sonata_tasks.tasks.models import TaskResult
from sonata_tasks.vm.models import VmInfo, VmRequest


@dataclass
class RecordingShell:
    commands: list[tuple[str, ...]] = field(default_factory=list)

    def run(self, argv: list[str], *, cwd, env, dry_run: bool) -> TaskResult:
        self.commands.append(tuple(argv))
        return TaskResult(task_id="x", status="passed", return_code=0)


@dataclass
class FakeOrchestrator:
    shell: RecordingShell = field(default_factory=RecordingShell)
    ensured: list[VmRequest] = field(default_factory=list)
    destroyed: list[str] = field(default_factory=list)

    def ensure_running(self, request: VmRequest) -> object:
        self.ensured.append(request)
        return _Result(return_code=0)

    def connection_host(self, request: VmRequest) -> str:
        return "10.0.0.5"

    def teardown(self, request: VmRequest) -> None:
        self.destroyed.append(request.name or "?")


@dataclass
class _Result:
    return_code: int


def test_provision_roles_ensures_runs_operations_and_destroys(tmp_path) -> None:
    provider = FakeOrchestrator()
    request = VmRequest(lifecycle="multipass", name="stack")
    op = RemoteCommandOperation(operation_id="k3s", summary="install", argv=("helm", "install"))
    with provision_roles(
        provider,
        (ProvisionedRole(role="stack", request=request, operations=(op,)),),
        repo_root=tmp_path,
        assets_root=tmp_path / "assets",
    ):
        pass
    assert [r.name for r in provider.ensured] == ["stack"]
    assert provider.shell.commands == [("helm", "install")]
    assert provider.destroyed == ["stack"]


def test_provision_roles_keep_skips_teardown(tmp_path) -> None:
    provider = FakeOrchestrator()
    request = VmRequest(lifecycle="multipass", name="stack")
    with provision_roles(
        provider,
        (ProvisionedRole(role="stack", request=request),),
        repo_root=tmp_path,
        assets_root=tmp_path / "assets",
        keep=True,
    ):
        pass
    assert provider.destroyed == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/provisioning/test_environment.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

Create `packages/sonata-tasks/src/sonata_tasks/provisioning/environment.py` — transcribe `provision_environment` (lines 238-410 of `packages/nanolab/src/nanolab/cli/provisioning.py`) with these changes:

- Replace the `orchestrator_factory`/`environment.provider` branching with a caller-supplied `provider` (nanolab computes it via `provider_for`).
- Replace per-role request/operations construction (config-driven, lines 250-375) with the `roles: tuple[ProvisionedRole, ...]` parameter. The composite is THREE-PHASE, reproducing the legacy order exactly: (1) ensure every role's VM in role order via `VmLifecycleAdapter`+`EnsureVmRunning` (each inside `workflow_step`, registering its destroy task BEFORE its ensure); (2) run `after_ensure(role, request)` for each role in order; (3) run each role's operations in order via `retarget_cloud_operations(provider, context, operations)` + `run_bootstrap_operations(provider, retargeted, role=role)` with `context = scenario_context(repo_root, resolved, assets_root)`. If any ensure fails, no after_ensure and no operations run.
- The `finally` teardown keeps the legacy semantics exactly: destroy each role in reverse order via `DestroyVm` unless `keep`, each destroy inside its OWN try/except (a failed destroy does NOT stop the remaining destroys — errors are collected and aggregated), `workflow_step` wrapping, and the error aggregation copied verbatim from `Workflow.run` (`"Cleanup failed:\n"` + joined errors for the cleanup-only case; combined message from the main error otherwise).
- Keep `_destroy_task` as a private helper (same body, using sonata `VmLifecycleAdapter`/`DestroyVm`/`VmInfo`/`vm_remote_home`).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/provisioning/test_environment.py -q`
Expected: PASS

- [ ] **Step 5: Re-export from the package**

Extend `packages/sonata-tasks/src/sonata_tasks/provisioning/__init__.py`:

```python
from sonata_tasks.provisioning.environment import ProvisionedRole, provision_roles
```

and add both names to `__all__`.

- [ ] **Step 6: Run full sonata suite**

Run: `pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -q`
Expected: PASS, coverage ≥ 90%

- [ ] **Step 7: Commit**

```bash
git add packages/sonata-tasks
git commit -m "feat: multi-role provisioning composite in sonata_tasks"
```

---

### Task 5: Rebuild nanolab's provision_environment on the sonata composite

`packages/nanolab/src/nanolab/cli/provisioning.py` becomes a config-translation shim: `EnvironmentConfig`/`ScenarioConfig` → `ProvisionedRole` tuples + `provider_for`, delegating to `sonata_tasks.provisioning.provision_roles`. `vm_provider.py` keeps only `vm_request_for_role` (config translation). The 13 tests in `packages/nanolab/tests/test_provisioning.py` keep passing unchanged in behavior.

**Files:**
- Modify: `packages/nanolab/src/nanolab/cli/provisioning.py` (rewrite)
- Modify: `packages/nanolab/src/nanolab/cli/vm_provider.py` (remove `vm_provider_for_environment`; re-point its caller in `cli/execution.py`)
- Modify: `packages/nanolab/src/nanolab/cli/execution.py` (use `provider_for` directly)

**Interfaces:**
- Consumes: everything from Tasks 1-4.
- Produces: `provision_environment(scenario, environment, *, repo_root, orchestrator_factory=None, post_ensure_verifier=None, keep=False)` — same signature as today (callers `product.py:526`, `tui/app.py:462` unchanged).

- [ ] **Step 1: Rewrite provisioning.py as a config shim**

Rewrite `packages/nanolab/src/nanolab/cli/provisioning.py` to:

```python
"""Provisioning: translate nanolab config onto sonata_tasks provisioning resources."""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

from sonata_tasks.components.operations import RemoteCommandOperation, ScenarioOperation
from sonata_tasks.provisioning import (
    ProvisionedRole,
    provider_for,
    provision_roles,
    remote_operations,
)
from sonata_tasks.vm.models import VmRequest

from nanolab.cli.vm_provider import vm_request_for_role
from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.config.environment import ExecutionRole
from nanolab.workspace.paths import discover_tool_root


def _stack_operations(
    scenario: ScenarioConfig,
    context,
    *,
    dedicated_loadgen: bool,
    include_repo_sync: bool = True,
) -> tuple[RemoteCommandOperation, ...]:
    # Same planner composition as before — the planners live in
    # sonata_tasks.components.bootstrap; context is a ScenarioExecutionContext.
    ...
```

Keeping verbatim: `_stack_operations` (unchanged body from today), `_request` (delegating to `vm_request_for_role`), the role-selection logic (`loadtest_workflow`, `dedicated_loadgen`, `dedicated_cloud`, `dedicated_arm`), and the `provision_environment` contextmanager — now:

```python
@contextmanager
def provision_environment(
    scenario: ScenarioConfig,
    environment: EnvironmentConfig,
    *,
    repo_root: Path,
    orchestrator_factory: Callable[[Path], Any] | None = None,
    post_ensure_verifier: Callable[[ExecutionRole, VmRequest], None] | None = None,
    keep: bool = False,
) -> Generator[None, None, None]:
    if environment.provider == "local":
        raise ValueError("--provision requires a non-local environment")
    if orchestrator_factory is not None:
        provider = orchestrator_factory(repo_root)
    else:
        provider = provider_for(vm_request_for_role(environment, "stack"), repo_root)

    roles: list[ProvisionedRole] = []
    for role, request, ops in _role_requests_and_operations(scenario, environment):
        roles.append(ProvisionedRole(role=role, request=request, operations=ops))

    with provision_roles(
        provider,
        tuple(roles),
        repo_root=repo_root,
        assets_root=discover_tool_root() / "assets",
        keep=keep,
        after_ensure=(
            (lambda role, request: post_ensure_verifier(role, request))
            if post_ensure_verifier is not None
            else None
        ),
    ):
        yield
```

The helper `_role_requests_and_operations` computes the same per-role tuples the legacy body did (lines 250-375: `_request(environment, role, loadtest=...)`, `_stack_operations(...)`, loadgen/cloud operation sets), returning `(role, request, operations)` triples. `_destroy_task`/`_ensure_vm`/`_run_operations`/`_context`/`_remote_operations`/`_retarget_cloud_operations`/`_OperationTask` are deleted (their sonata homes exist).

- [ ] **Step 2: Run the provisioning tests**

Run: `NANOFAAS_ROOT=$HOME/Downloads/mcFaas pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/test_provisioning.py -q`
Expected: PASS — all 14 tests, same behavior. If a test asserted a private helper name, re-point the import to the sonata module (e.g. `sonata_tasks.provisioning.remote_operations`).

- [ ] **Step 3: Remove the old factory from vm_provider.py**

In `packages/nanolab/src/nanolab/cli/vm_provider.py`: delete `vm_provider_for_environment` and its `provider_for`/`Path` imports if unused; keep `vm_request_for_role` untouched.

In `packages/nanolab/src/nanolab/cli/execution.py`: replace the `vm_provider_for_environment(environment, default_tool_paths().nanofaas_root)` call with:

```python
from sonata_tasks.provisioning.providers import provider_for
...
provider = vm_provider or provider_for(
    vm_request_for_role(environment, "stack", loadtest=True),
    default_tool_paths().nanofaas_root,
)
```

- [ ] **Step 4: Run full nanolab suite**

Run: `NANOFAAS_ROOT=$HOME/Downloads/mcFaas pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q`
Expected: PASS (pre-existing environment failure allowed)

- [ ] **Step 5: Commit**

```bash
git add packages/nanolab/src/nanolab/cli/provisioning.py packages/nanolab/src/nanolab/cli/vm_provider.py packages/nanolab/src/nanolab/cli/execution.py packages/nanolab/tests
git commit -m "refactor: nanolab provisioning delegates to sonata_tasks resources"
```

---

### Task 6: Delete workflow-tasks

`workflow_tasks.core` has no remaining consumers once Task 5 lands. Delete the package, its dependency, and the tests that pin it.

**Files:**
- Delete: `packages/workflow-tasks/` (entire directory)
- Delete: `packages/workflow-tasks/.importlinter`
- Modify: `packages/nanolab/pyproject.toml` (remove `"workflow-tasks"` from dependencies)
- Modify: root `pyproject.toml` (remove `packages/workflow-tasks` from `[tool.uv.workspace].members`)
- Modify: `packages/sonata-tasks/.importlinter` (drop `workflow_tasks` from `root_packages` and the `no_legacy_engine` contract)
- Modify: `.github/workflows/ci.yml` (drop the `lint-imports --config packages/workflow-tasks/.importlinter` line)

- [ ] **Step 1: Verify zero remaining imports**

Run: `grep -rn "workflow_tasks" packages/*/src packages/*/tests --include="*.py" | grep -v "\.egg" | grep -v __pycache__`
Expected: no output (comments about the migration history are fine — delete them if any remain)

- [ ] **Step 2: Delete the package and update configs**

```bash
git rm -r packages/workflow-tasks
```

- In `packages/nanolab/pyproject.toml`: remove the `"workflow-tasks",` line from `dependencies`.
- In root `pyproject.toml`: remove `"packages/workflow-tasks",` from `[tool.uv.workspace].members`.
- In `packages/sonata-tasks/.importlinter`: remove `workflow_tasks` from `root_packages` and the `[importlinter:contract:no_legacy_engine]` block.
- In `.github/workflows/ci.yml`: remove the line `uv run --locked --all-packages --all-groups lint-imports --config packages/workflow-tasks/.importlinter --no-cache`.

- [ ] **Step 3: Update uv lock**

Run: `uv lock`
Expected: lock resolves without `workflow-tasks`

- [ ] **Step 4: Run all suites**

Run: `pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -q` and `NANOFAAS_ROOT=$HOME/Downloads/mcFaas pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q`
Expected: PASS both (coverage gates ≥ 90%; pre-existing environment failure allowed)

- [ ] **Step 5: Run import-linter**

Run: `lint-imports --config packages/sonata-tasks/.importlinter --no-cache` and `lint-imports --config packages/nanolab/.importlinter --no-cache`
Expected: contracts kept, 0 broken

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: delete the workflow-tasks package"
```

---

## Self-Review

**Spec coverage:** The user's direction — provisioning used only by tasks/resources, well-circumscribed resources absorbing the providers — maps to Tasks 1-4 (provider factory absorbs `vm_provider_for_environment`; resources absorb `_VerifiedLifecycle`/`_vm`; bootstrap absorbs the operation helpers; composite absorbs the contextmanager). Task 5 keeps nanolab config translation at the boundary (import-linter constraint). Task 6 delivers the long-term goal: workflow-tasks gone. The 14 provisioning tests are the behavior contract and are never weakened.

**Placeholder scan:** The only intentional ellipsis is `_stack_operations`' body in Task 5 Step 1, marked "unchanged body from today" — the engineer reads the existing function at `packages/nanolab/src/nanolab/cli/provisioning.py` before the rewrite. All new code is given in full.

**Type consistency:** `provider_for(request, repo_root)` (Task 1) is consumed in Tasks 4-5; `provisioned_vm(*, title, request, provider, after_ensure, requires)` (Task 2) matches the release call sites exactly (keyword args identical); `scenario_context(repo_root, request, assets_root)` is used in Tasks 3-4; `ProvisionedRole(role, request, operations)` and `provision_roles(provider, roles, *, repo_root, assets_root, keep, after_ensure)` (Task 4) are consumed in Task 5. `retarget_cloud_operations(orchestrator, context, operations)` drops the `environment` parameter in every call site it touches (Task 3 steps 5, Task 4 step 3). `after_ensure` is `Callable[[str, VmRequest], None]` in Task 4 and `Callable[[ExecutionRole, VmRequest], None]` in the Task 5 shim — `ExecutionRole` is a `str` alias, so the lambda `(lambda role, request: post_ensure_verifier(role, request))` is consistent.
