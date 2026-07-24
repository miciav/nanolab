# nanolab

`miciav/nanolab` is the standalone home for the operational tooling extracted
from nanofaas. It contains three Python workspace members:

- `packages/nanolab`: the nanofaas operations CLI and supporting tooling
- `packages/workflow-tasks`: reusable workflow task primitives
- `packages/tui-toolkit`: shared terminal UI components

The initial source snapshot comes from nanofaas commit
`4e0aa0751b5f3a5008012994bd4a8843de801316`. Its Git history was intentionally
not preserved.

## Development

Point the tooling at a nanofaas checkout before running repository-dependent
commands:

```bash
export NANOFAAS_ROOT=/path/to/nanofaas
```

The root quality gates will be:

```bash
uv lock --check
uv sync --locked --all-packages --all-groups
uv run --locked --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests
uv run --locked --all-packages --all-groups pytest -c packages/workflow-tasks/pyproject.toml packages/workflow-tasks/tests
uv run --locked --all-packages --all-groups pytest -c packages/tui-toolkit/pyproject.toml packages/tui-toolkit/tests
uv run --locked --all-packages --all-groups ruff check packages
uv run --locked --all-packages --all-groups basedpyright --project packages/nanolab
uv run --locked --all-packages --all-groups basedpyright --project packages/workflow-tasks
uv run --locked --all-packages --all-groups basedpyright --project packages/tui-toolkit
uv run --locked --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
uv run --locked --all-packages --all-groups lint-imports --config packages/workflow-tasks/.importlinter --no-cache
uv run --locked --all-packages --all-groups lint-imports --config packages/tui-toolkit/.importlinter --no-cache
uv build --all-packages --out-dir dist --clear
```

The workspace and shared quality configuration needed by these commands are
added in the next extraction steps.
