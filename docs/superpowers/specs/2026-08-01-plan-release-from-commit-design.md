# Plan the Release From the Commit, Not the Worktree

## Problem

A release ships a source archive produced by `git archive <commit>`, so the bytes
built on the VMs are exact to the guarded commit. But *what to build* — the image
matrix — is computed separately, by scanning the local working checkout:

```python
# nanolab/functions/catalog.py
def _load_functions():
    paths = default_tool_paths()                       # reads NANOFAAS_ROOT
    _discover_example_functions(paths.nanofaas_root / "functions", ...)
```

`_discover_example_functions` treats **any subdirectory** of `functions/<runtime>/`
as a function. So the plan and the archive can disagree.

The clean-tree guard covers most of the gap: `git status --porcelain` reports
untracked files, so a new uncommitted function makes the tree dirty and
`create_source_archive` refuses. What it does not cover is **gitignored** paths.

Observed on 2026-08-01: switching the nanoFaaS checkout off `feat/spring-boot-4.1`
left `functions/java/figlet/` behind as gitignored build output (`build/`,
`payloads/`, no `Dockerfile`). The catalog discovered a phantom function and the
release preflight failed with `FileNotFoundError: missing image Dockerfile:
functions/java/figlet/Dockerfile`. 132 tests failed for the same reason; against a
pristine worktree of the same commit, all 818 passed.

The failure is caught offline in about a second, so the cost today is low. The goal
of this change is not to repair an open wound: it is to make the class of error
**impossible to express** rather than merely detectable, and to collapse the mental
model to one source of truth.

## Goal

Every input that decides *what the release builds* derives from the guarded commit.
The working checkout stays authoritative only for what is legitimately its own.

## Design

### Extraction

`build_release_request` receives an empty directory and extracts the guarded commit
into it, then plans from that tree:

```
build_release_request(..., source_tree: Path)
  ├─ git archive <commit> | tar -x  →  source_tree
  ├─ build_image_plan(source_tree, ...)
  └─ ReleaseRequest(source_tree=..., ...)

build_release_workflow(request)
  ├─ build_arm64_image_plan(request.source_tree, ...)
  └─ build_publish_plan(request.source_tree, ...)
```

`source_tree` is a **required** keyword argument: there is no implicit fallback to
the working checkout, because a silent fallback would restore exactly the ambiguity
this change removes. Both `run` and `plan` supply one — `plan` needs the matrix too,
and must stay offline while computing it.

`ReleaseRequest` gains a `source_tree: Path` field so the workflow builder plans
from the same tree as the preflight.

### What moves, and what does not

**From the commit** (the extraction): the AMD64 image matrix, the ARM64 image
matrix, the publication plan.

**From the working checkout** (unchanged):

| Concern | Why it stays |
|---|---|
| `git_state`, `verify_version_consistency` | need a real repository; an extraction has no `.git` |
| `credentials.validate(repo_root=...)` | that path is exactly what the guard must police |
| Ansible bootstrap, `build_role_bindings` | consume tooling assets, not release source |
| `create_source_archive` | needs `.git` to archive the commit |
| `performance_root` (`docs/performance`) | an **output**: `finalize` writes the record and history there |

### Catalog rooting

```python
def list_functions(root: Path | None = None) -> list[FunctionDefinition]: ...
def resolve_function_definition(key: str, root: Path | None = None) -> FunctionDefinition: ...
```

`None` keeps today's behaviour (the `NANOFAAS_ROOT` global), so `plans/validate.py`
and every non-release workflow are untouched.

`build_image_plan` passes its own `repo_root` to `list_functions` instead of
leaving it implicit. This also closes a latent assumption: `_function_target` does
`example_dir.resolve().relative_to(repo_root)`, which today only works because the
catalog root and `repo_root` happen to be the same directory. Divergence currently
raises an opaque `ValueError` from `relative_to`.

`resolve_function_definition` at **run time** — `_function_target_name` in
`release/benchmark.py`, used to pin benchmark images — keeps reading the global. It
maps a function key to a name and touches no paths. If the two sources ever
diverged it already fails closed with a clear message (`release image plan has no
AMD64 native image for X`). Threading a root through the task layer costs more than
it protects.

### Lifetime

The caller owns the temporary tree: a `tempfile.TemporaryDirectory` held in an
`ExitStack` in `cli/product.py`, closed in a `finally`. It is needed from preflight
until the workflow is compiled; nothing reads it afterwards. Tests pass `tmp_path`.
No `atexit`, no hidden lifetime, nothing left on disk.

### Archive sharing: deliberately not done

The preflight extraction and the archive the resource ships to the VMs are produced
by two separate `git archive` invocations of the same commit. They are equivalent:
`git archive` is deterministic for a given commit, and `create_source_archive`
re-checks the git state and the commit and refuses if either moved. Sharing one
artifact would couple the resource's lifetime to the preflight for a few seconds of
saved work. The reproducibility property holds either way.

### Errors

A failed extraction raises `ValueError` from the preflight, which the CLI converts
to `typer.BadParameter` — offline, before any cloud resource, like every other
preflight failure.

## Testing

- `list_functions(root)` discovers from the given root; two different roots in one
  test prove there is no cross-talk, and the default still reads the global.
- **Regression test for the observed bug**: a checkout carrying a leftover
  gitignored `functions/java/figlet/build/` produces no cell for it. Today the same
  input raises `FileNotFoundError`.
- A new, uncommitted function directory complete with a `Dockerfile` does not enter
  the matrix — the case the clean-tree guard covers behaviourally and this change
  covers structurally.
- The preflight stays offline: with a provider whose every method raises, `plan`
  still compiles the full DAG.

## Out of scope

- Making non-release workflows plan from a commit. They operate on the working
  checkout by design.
- Cloning on the VMs instead of shipping an archive. It would not fix the matrix,
  needs a git token on three VMs, adds a GitHub dependency inside the paid window,
  and loses the checksum-identical-archive property the ARM64 phase asserts.
