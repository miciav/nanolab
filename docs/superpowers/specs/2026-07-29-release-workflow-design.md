# Release Workflow — Sonata

## Scope

Replace the procedural `nanolab release` (~2200 lines in `release/run.py`) with a
Sonata `Workflow` compiled by `build_release_workflow()`, same pattern as
`build_loadtest_plan()`.

**Constraints:** Azure only. Single linear workflow. Composite image tasks.

## Architecture

```
build_release_workflow(request: ReleaseRequest) → Workflow
```

`ReleaseRequest` is a dataclass holding: `repo_root`, `version`, `environment`
(Azure), `scenario` (loadtest), `settings` (ReleaseSettings), `image_plan`
(ImagePlan, 52 cells), `credentials` (CredentialFiles), `run_dir`,
`performance_root`.

The workflow is 12 top-level nodes. Each node is either a `Resource`
(acquire/release), a `Steps` composite (multiple sub-tasks), or a pure `Task`.

## New Tasks/Resources

All new code lives in `packages/sonata-tasks/src/sonata_tasks/`.

| Module | Type | Purpose |
|--------|------|---------|
| `archive.py` | Resource | `git archive` → transfer → sha256 verify → `tar -xf` |
| `buildx.py` | Resource | `docker buildx create/bootstrap` / `buildx rm --force` |
| `registry_tunnel.py` | Resource | `socat` tunnel ARM builder → stack registry |
| `transfer.py` | Task | Transfer a local file to a remote VM |
| `skopeo.py` | Task | `skopeo copy --preserve-digests` + digest verify |
| `imagetools.py` | Task | `docker buildx imagetools create` for manifests/aliases |
| `syft.py` | Task | Containerised Syft SBOM generation |
| `cosign.py` | Task | `cosign sign/attest/attach/verify` with staged credentials |
| `release_metrics.py` | Task | `aggregate_runs()` + `evaluate_regression()` wrappers |

## Workflow Phases

1. **SourceTests** — SourceArchive Resource + 6-step composite (gradle, python, go, node, rust, bash)
2. **Amd64Build** — BuildxBuilder Resource + composite: prepare JVM × N, bake docker, gradle native × N
3. **RegistryPush** — Composite: per image: `docker push` + `skopeo inspect` digest
4. **Benchmark-1** — Reuses `build_loadtest_plan()` (zero new code)
5. **Benchmark-2** — Same
6. **Benchmark-3** — Same
7. **Aggregate** — Pure Task: reads 3 summary.json, calls `aggregate_runs()`
8. **RegressionGate** — Pure Task: `evaluate_regression()` against baseline
9. **Arm64Build** — RegistryTunnel Resource + BuildxBuilder Resource + composite
10. **Arm64Smoke** — Composite: per server image health-check + watchdog
11. **Publish** — Three composites: architectures (skopeo), manifests (imagetools), aliases (imagetools)
12. **Attest+Finalize** — Composite: syft + cosign per digest, then `finalize_release()`

## Composite Image Tasks

Every image-list operation is a single `Steps` node whose children are one step
per image cell. Example:

```python
def _registry_push(plan: ImagePlan, executor, role) -> Steps:
    return Steps(*[
        Steps(
            DockerPushTask(image=cell.image, executor=executor, role=role),
            SkopeoInspectTask(image=cell.image, executor=executor, role=role),
        )
        for cell in plan.cells
    ])
```

## Reuse

- `ReleaseJournal`, `ArtifactEvidence` — unchanged
- `aggregate_runs()`, `evaluate_regression()` — unchanged  
- `source_test_commands()`, `amd64_build_commands()`, `arm64_build_commands()` — unchanged, mapped 1:1 to `CommandTask`
- `build_publish_plan()`, `publish_*()`, `attest_release_images()`, `finalize_release()` — unchanged, wrapped in Tasks
- `build_loadtest_plan()` — reused directly for benchmarks
- `RoleBoundCommandTaskExecutor`, `CommandTask` — used everywhere
