# nanolab

`miciav/nanolab` is the standalone home for the operational tooling extracted
from nanofaas. It contains three Python workspace members:

- `packages/nanolab`: the nanofaas operations CLI and supporting tooling
- `packages/sonata-tasks`: reusable workflow task primitives
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

For example:

```bash
NANOFAAS_ROOT=/path/to/nanofaas uv run --package nanolab nanolab plan packages/nanolab/scenarios-v2/deployment-lifecycle-container.yaml
NANOFAAS_ROOT=/path/to/nanofaas uv run --package nanolab nanolab run packages/nanolab/scenarios-v2/deployment-lifecycle-container.yaml
```

Or use the bundled launcher, which checks for `uv` and forwards all
arguments — equivalent to `uv run --package nanolab nanolab ...`:

```bash
export NANOFAAS_ROOT=/path/to/nanofaas
./nanolab.sh plan packages/nanolab/scenarios-v2/deployment-lifecycle-container.yaml
```

## CI gate

`.github/workflows/ci.yml` runs on every push and pull request against
`main`. It checks out this repo, checks out the pinned nanoFaaS source
snapshot (`miciav/nanofaas` at `c6fa85f6399265e3e6284a8f57a66e2814f7f753`,
which has moved on from the initial snapshot above) into `.nanofaas-source`, points
`NANOFAAS_ROOT` at that checkout, and runs the full gate below.
`miciav/nanofaas` is private, so the cross-repo checkout authenticates with
the repository secret `NANOFAAS_CHECKOUT_TOKEN` (a fine-grained PAT scoped to
read-only Contents access on `miciav/nanofaas`) rather than the default
`GITHUB_TOKEN`. To reproduce it locally, run the same commands in the same
order against a local nanoFaaS checkout:

```bash
export NANOFAAS_ROOT=/path/to/nanofaas   # e.g. your working mcFaas checkout

uv lock --check
uv sync --locked --all-packages --all-groups

uv run --locked --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests
uv run --locked --all-packages --all-groups pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests
uv run --locked --all-packages --all-groups pytest -c packages/tui-toolkit/pyproject.toml packages/tui-toolkit/tests

uv run --locked --all-packages --all-groups ruff check packages
uv run --locked --all-packages --all-groups basedpyright --project packages/nanolab
uv run --locked --all-packages --all-groups basedpyright --project packages/sonata-tasks
uv run --locked --all-packages --all-groups basedpyright --project packages/tui-toolkit

uv run --locked --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
uv run --locked --all-packages --all-groups lint-imports --config packages/sonata-tasks/.importlinter --no-cache
uv run --locked --all-packages --all-groups lint-imports --config packages/tui-toolkit/.importlinter --no-cache

uv build --all-packages --out-dir dist --clear

uv venv .wheel-smoke
uv pip install dist/nanolab-0.1.0-py3-none-any.whl dist/sonata_tasks-0.1.0-py3-none-any.whl dist/tui_toolkit-0.1.0-py3-none-any.whl --python .wheel-smoke/bin/python
.wheel-smoke/bin/python -c "import nanolab, sonata_tasks, tui_toolkit"
.wheel-smoke/bin/nanolab --help

uv run --package nanolab nanolab plan packages/nanolab/scenarios-v2/deployment-lifecycle-container.yaml --environment packages/nanolab/environments/local.yaml
uv run --package nanolab nanolab plan packages/nanolab/scenarios-v2/deployment-lifecycle-k8s.yaml --environment packages/nanolab/environments/multipass.yaml
```

Nothing in this gate should modify `uv.lock`; if it does, run `uv lock` and
commit the updated lockfile separately.

## Local SonarQube analysis

With Docker running and `sonar-scanner` installed (`brew install sonar-scanner`
on macOS), scan all three workspace packages with:

```bash
./scripts/sonar.sh
```

The script starts an ephemeral SonarQube Community container on
`127.0.0.1:9000`, analyses `packages/nanolab/src`,
`packages/sonata-tasks/src`, and `packages/tui-toolkit/src` together with their
test trees, then prints the open issue counts. It leaves the server running so
the findings remain browsable and writes their complete API response to
`.scannerwork/issues.json`. Use `./scripts/sonar.sh --rm` to remove the server
after the scan, or `./scripts/sonar.sh --dry-run` to inspect the scanner command.

### Using a newer nanoFaaS checkout

CI always pins `NANOFAAS_ROOT` to the nanoFaaS commit recorded above, so the
gate stays reproducible. For local development against a newer nanoFaaS
checkout (e.g. picking up source changes that haven't been re-pinned yet),
just point `NANOFAAS_ROOT` at that checkout instead:

```bash
NANOFAAS_ROOT=/path/to/newer/nanofaas uv run --package nanolab nanolab plan packages/nanolab/scenarios-v2/deployment-lifecycle-container.yaml
```

Bumping the pin used by CI means updating both the `ref:` in
`.github/actions/setup-workspace/action.yml` and the commit noted in this
README; `test_readme_quotes_the_nanofaas_commit_ci_actually_pins` fails if you
change one and forget the other.
