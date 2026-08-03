# Release Workflow Remediation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Destinazione finale nel repo:** `docs/superpowers/plans/2026-08-03-release-workflow-remediation.md`. Il piano vive qui perché la plan mode consente di scrivere solo il file di piano; copiarlo nel repo come primo atto dell'esecuzione.

**Goal:** Riportare la build AMD64 del percorso Sonata alla fedeltà della build legacy/ARM64, dare granularità di resume ed evidenza di firma alla fase `attest`, e lasciare un solo percorso di esecuzione con un solo journal.

**Architecture:** Il workflow di release è un DAG Sonata compilato da `build_release_workflow` (`packages/nanolab/src/nanolab/plans/release.py:245`), 15 fasi tutte istanze di `ReleasePhaseTask` (`packages/nanolab/src/nanolab/release/tasks.py:39`) il cui lavoro reale è un callable `work=`. Alcune fasi eseguono composite `Steps` di sonata-tasks, altre chiamano funzioni procedurali di `nanolab/release/`. Questo piano sposta AMD64 sul generatore di comandi già testato (`amd64_build_commands`) invece del composite riscritto e incompleto, porta `attest` su un composite per-immagine, e poi cancella runner legacy e composite morti.

**Tech Stack:** Python 3.12, uv workspace a 4 pacchetti, `sonata-engine` (git pin), pytest, basedpyright, ruff, import-linter, Typer, Docker/buildx/skopeo/syft/cosign su VM Azure.

## Global Constraints

- **Test:** eseguire secondo `memory/nanolab-tests-need-pinned-nanofaas.md` — `NANOFAAS_ROOT` deve essere impostato, pytest si lancia **per pacchetto** con il suo `-c`, e nessuna asserzione deve contenere conteggi di celle hardcoded (il catalogo immagini cambia). Esempi di invocazione in `.github/workflows/ci.yml:60-70`.
- **Pin del motore:** un solo rev di `sonata-engine` in tutto il workspace, quello della root (`pyproject.toml:21`, `818d53e6375f71930eb0a198ffb1dfc84cc6b077`).
- **Direzione delle dipendenze:** `sonata-tasks` **non può importare** `nanolab`. Contratti verificati da `lint-imports --config packages/<pkg>/.importlinter`. Ogni task che tocca entrambi i pacchetti deve rieseguire `lint-imports`.
- **Nessun segreto in argv:** password cosign e token GHCR passano solo tramite file + wrapper `sh -c`, mai come argomento di processo. Vedi `packages/sonata-tasks/src/sonata_tasks/cosign.py:113-122` e `packages/nanolab/src/nanolab/release/attest.py:285-292`.
- **Immagini pinnate per digest:** `SYFT_IMAGE` (`syft.py:13`) e `COSIGN_IMAGE` (`cosign.py:13`) non si aggiornano in questo piano.
- **Commit frequenti:** un commit per task, mai un commit che lascia i test rossi.

---

## File Structure

**Nuovi file**

| File | Responsabilità |
|---|---|
| `packages/nanolab/src/nanolab/release/model.py` | Le dataclass di piano condivise tra percorso Sonata e (per poco) runner legacy: `GitState`, `CredentialFiles`, `BuilderConfiguration`, `ReleaseSettings`, `Amd64ReleasePlan`, `ReleaseIdentity`, `digest_path`, `git_state`. Esistono oggi dentro `release/run.py` e `release/state.py`, che questo piano cancella. |

**File modificati e loro responsabilità dopo il piano**

| File | Dopo |
|---|---|
| `packages/sonata-tasks/src/sonata_tasks/release_composites.py` | Solo 4 composite: `command_specs_composite` (generico spec→Steps), `registry_push_composite`, `attest_composite` (completo, 5 operazioni), e nient'altro. Da 677 righe a ~250. |
| `packages/sonata-tasks/src/sonata_tasks/cosign.py` | `CosignTask` con i flag corretti (`--yes`, `--type custom`, `--type spdx`) e l'operazione `public-key`. |
| `packages/nanolab/src/nanolab/release/build.py` | `amd64_build_commands` prende `ImagePlan` + `builder_name` (simmetrico ad `arm64_build_commands`), senza creare il builder. Spariscono le funzioni del runner procedurale. |
| `packages/nanolab/src/nanolab/release/tasks.py` | `run_image_steps` verifica l'architettura sul ramo locale. |
| `packages/nanolab/src/nanolab/release/resources.py` | `build_inputs_resource` generalizzata: stage di bake+buildkitd per stack **e** arm. |
| `packages/nanolab/src/nanolab/release/attest.py` | Solo `build_release_predicate`, `render_predicate`, `finalize_release`, `performance_root`, helper. Spariscono `attest_release_images`, `_cosign`, `_write_public_key`. |
| `packages/nanolab/src/nanolab/plans/release.py` | Unico compilatore del DAG. |
| `packages/nanolab/src/nanolab/cli/release.py` | Solo `release prepare`. |
| **Cancellati** | `release/run.py` (1182 righe), `release/state.py` (453 righe), `tests/release/test_run_amd64.py`, `tests/release/test_state.py` |

---

## Task 1: Allineare il pin di sonata-engine

`packages/sonata-tasks/pyproject.toml:7` pinna `sonata-engine @ git+…@c2ae952c…`, la root pinna `818d53e6…`. Nel workspace vince la root via `uv.lock`, quindi oggi è dormiente — ma la CI costruisce e installa i wheel singolarmente (`.github/workflows/ci.yml:100`), e chi installa `sonata_tasks` fuori dal workspace prende un motore diverso da quello contro cui il codice è testato.

**Files:**
- Modify: `packages/sonata-tasks/pyproject.toml:7`

**Interfaces:**
- Consumes: niente
- Produces: niente (cambio di packaging)

- [ ] **Step 1: Verificare la divergenza**

```bash
grep -n "sonata-engine" pyproject.toml packages/sonata-tasks/pyproject.toml
```

Atteso: due rev diversi, `c2ae952c6d201f2e1b997df3fd301480e170185f` e `818d53e6375f71930eb0a198ffb1dfc84cc6b077`.

- [ ] **Step 2: Allineare al rev della root**

In `packages/sonata-tasks/pyproject.toml`, sostituire la riga 7:

```toml
    "sonata-engine @ git+https://github.com/miciav/sonata.git@818d53e6375f71930eb0a198ffb1dfc84cc6b077",
```

- [ ] **Step 3: Ricompilare il lock e verificare che non cambi nulla**

```bash
uv lock
git diff --stat uv.lock
```

Atteso: `uv.lock` invariato o con solo il metadato del requirement aggiornato. Se cambia la versione risolta di `sonata-engine`, **fermarsi**: significa che il workspace non stava usando il rev della root e va indagato prima di procedere.

- [ ] **Step 4: Smoke dei wheel come fa la CI**

```bash
uv build --all-packages --out-dir dist
uv venv .wheel-smoke && uv pip install dist/*.whl --python .wheel-smoke/bin/python
.wheel-smoke/bin/python -c "import nanolab, sonata_tasks, workflow_tasks, tui_toolkit"
rm -rf .wheel-smoke dist
```

Atteso: nessun errore.

- [ ] **Step 5: Commit**

```bash
git add packages/sonata-tasks/pyproject.toml uv.lock
git commit -m "chore: pin one sonata-engine rev across the workspace"
```

---

## Task 2: Generalizzare `source_tests_composite` in `command_specs_composite`

`source_tests_composite` (`release_composites.py:76`) non ha niente di specifico dei test: è già un wrapper generico `Sequence[CommandTaskSpec] → Steps` che propaga `argv`, `env`, `cwd`, `remote_dir`, `role`, `expected_exit_codes`, `timeout_seconds`. Rinominarlo è il prerequisito del Task 4, che lo riuserà per la build AMD64 invece di riscrivere i comandi.

Rinomina pura: nessun cambiamento di comportamento.

**Files:**
- Modify: `packages/sonata-tasks/src/sonata_tasks/release_composites.py:76-114`
- Modify: `packages/sonata-tasks/src/sonata_tasks/release_composites.py:41-49` (`__all__`)
- Modify: `packages/sonata-tasks/src/sonata_tasks/__init__.py` (la lista di re-export)
- Modify: `packages/nanolab/src/nanolab/plans/release.py:19-23,305`
- Test: `packages/sonata-tasks/tests/test_release_composites.py`

**Interfaces:**
- Consumes: `CommandTaskSpec` da `workflow_tasks.tasks.models`
- Produces: `command_specs_composite(commands: Sequence[CommandTaskSpec], executor: CommandTaskExecutor, *, title: str) -> Steps` — `title` diventa **obbligatorio come keyword** (non ha più un default sensato ora che serve a due fasi diverse). Consumata dai Task 4 e 6.

- [ ] **Step 1: Scrivere il test che fallisce**

In `packages/sonata-tasks/tests/test_release_composites.py`, aggiungere:

```python
def test_command_specs_composite_titles_each_step_from_the_spec_summary() -> None:
    executor = RecordingExecutor()
    commands = (
        CommandTaskSpec(task_id="a", summary="First", argv=("echo", "one"), role="stack"),
        CommandTaskSpec(task_id="b", summary="Second", argv=("echo", "two"), role="stack"),
    )

    composite = command_specs_composite(commands, executor=executor, title="Build AMD64 images")

    assert composite.title == "Build AMD64 images"
    assert [step.title for step in composite.steps] == ["First", "Second"]
```

E aggiungere `command_specs_composite` all'import in cima al file.

- [ ] **Step 2: Eseguire il test e verificare che fallisca**

```bash
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/sonata-tasks/pyproject.toml \
  packages/sonata-tasks/tests/test_release_composites.py::test_command_specs_composite_titles_each_step_from_the_spec_summary -v
```

Atteso: `ImportError: cannot import name 'command_specs_composite'`.

- [ ] **Step 3: Rinominare la funzione**

In `release_composites.py`, cambiare la firma e la docstring:

```python
def command_specs_composite(
    commands: Sequence[CommandTaskSpec],
    executor: CommandTaskExecutor,
    *,
    title: str,
) -> Steps:
    """Wrap a list of command specs into a Steps composite.

    Each ``CommandTaskSpec`` becomes a ``CommandTask``, run in order, journalled
    individually so a resumed phase skips the commands it already finished.

    This is the generic spec-to-Steps bridge: the release phases that already
    have a tested command generator (`source_test_commands`,
    `amd64_build_commands`) feed it here rather than rebuilding their argv.

    Parameters
    ----------
    commands :
        One spec per command.  ``spec.summary`` becomes the task title,
        ``spec.argv`` the command line, ``spec.role`` the execution role.
    executor :
        Role-bound executor that runs each command.
    title :
        Title of the composite, and the prefix of every step id in the journal.
    """
```

Il corpo (righe 100-114) resta identico. Aggiornare `__all__` sostituendo `"source_tests_composite"` con `"command_specs_composite"`, e fare lo stesso in `packages/sonata-tasks/src/sonata_tasks/__init__.py`.

- [ ] **Step 4: Aggiornare il chiamante in nanolab**

In `packages/nanolab/src/nanolab/plans/release.py`, nell'import a riga 19-23 sostituire `source_tests_composite` con `command_specs_composite`, e a riga 305:

```python
    source_steps = command_specs_composite(
        source_commands, executor=executor, title="Run source tests"
    )
```

- [ ] **Step 5: Aggiornare i test esistenti**

In `test_release_composites.py`, rinominare ogni occorrenza di `source_tests_composite` in `command_specs_composite` e aggiungere `title="Run source tests"` alle chiamate che si affidavano al default.

- [ ] **Step 6: Eseguire le suite e verificare che passino**

```bash
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests
```

Atteso: verde entrambe.

- [ ] **Step 7: Commit**

```bash
git add packages/sonata-tasks packages/nanolab/src/nanolab/plans/release.py
git commit -m "refactor: name the spec-to-Steps composite for what it does"
```

---

## Task 3: Rendere `amd64_build_commands` simmetrica ad `arm64_build_commands`

Oggi `amd64_build_commands` (`release/build.py:279`) prende un `Amd64ReleasePlan` (definito in `run.py`, che il Task 11 cancella) e genera anche i due comandi che creano il builder buildx — duplicando `buildx_builder_resource`, che il percorso Sonata usa già. `arm64_build_commands` (`release/arm.py:57`) è già nella forma giusta: prende `ImagePlan` + `builder_name`.

Questo task allinea la firma. Il comportamento del runner legacy resta identico perché il chiamante gli passa gli stessi valori.

**Files:**
- Modify: `packages/nanolab/src/nanolab/release/build.py:279-357`
- Modify: `packages/nanolab/src/nanolab/release/build.py:496-526` (`_build_amd64_images`, unico chiamante attuale)
- Test: `packages/nanolab/tests/release/test_build.py`

**Interfaces:**
- Consumes: `ImagePlan` da `nanolab.images.plan`, `CommandTaskSpec` da `workflow_tasks.tasks.models`
- Produces:
  ```python
  def amd64_build_commands(
      plan: ImagePlan,
      *,
      builder_name: str,
      remote_bake_file: str,
      remote_source_dir: str,
  ) -> tuple[CommandTaskSpec, ...]
  ```
  Consumata dal Task 4. Nota: **niente `remote_buildkit_config`** — la configurazione BuildKit è ora responsabilità del resource che crea il builder, non dei comandi di build.

- [ ] **Step 1: Scrivere il test che fallisce**

In `packages/nanolab/tests/release/test_build.py`, aggiungere. Usare il costruttore di piano reale, non un fake, così il test si rompe se il catalogo cambia forma — ma senza asserire conteggi:

```python
def test_amd64_build_commands_prepare_bake_and_build_natively(tmp_path: Path) -> None:
    plan = build_image_plan(_release_repo(tmp_path), "v9.9.9", architectures=("amd64",))

    commands = amd64_build_commands(
        plan,
        builder_name="release-amd64-9.9.9",
        remote_bake_file="/remote/docker-bake.json",
        remote_source_dir="/remote/source",
    )

    task_ids = [command.task_id for command in commands]
    # Il builder è di competenza del resource buildx, non di questi comandi.
    assert not any("buildx" in task_id for task_id in task_ids)
    # Ogni cella JVM bake ha il suo bootJar, prima del bake.
    prepares = [c for c in commands if c.task_id.startswith("release.images.prepare.")]
    assert prepares, "no JVM prerequisite generated"
    assert all(c.argv[0] == "./gradlew" for c in prepares)
    bake_index = task_ids.index("release.images.bake.amd64")
    assert all(task_ids.index(c.task_id) < bake_index for c in prepares)
    # Il bake usa il builder passato.
    bake = commands[bake_index]
    assert bake.argv == (
        "docker", "buildx", "bake",
        "--builder", "release-amd64-9.9.9",
        "--file", "/remote/docker-bake.json",
        "--load", "docker-amd64",
    )
    # Le celle native portano l'intero gradle_command, non un sottoinsieme.
    natives = [c for c in commands if c.task_id.startswith("release.images.native.")]
    assert natives, "no native build generated"
    for command, cell in zip(natives, plan.gradle_cells, strict=True):
        assert command.argv == cell.gradle_command
    assert all(c.role == "stack" and c.remote_dir == "/remote/source" for c in commands)
```

`_release_repo` è l'helper già presente in `packages/nanolab/tests/release/_release_support.py` che materializza un catalogo minimo; se non espone esattamente questo nome, usare quello che il file già fornisce per costruire un `ImagePlan`.

- [ ] **Step 2: Eseguire il test e verificare che fallisca**

```bash
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml \
  packages/nanolab/tests/release/test_build.py::test_amd64_build_commands_prepare_bake_and_build_natively -v
```

Atteso: FAIL — la firma attuale vuole `plan.image_plan` e `remote_buildkit_config`, e genera `release.buildx.create`.

- [ ] **Step 3: Cambiare la firma**

In `release/build.py`, sostituire le righe 279-357:

```python
def amd64_build_commands(
    plan: ImagePlan,
    *,
    builder_name: str,
    remote_bake_file: str,
    remote_source_dir: str,
) -> tuple[CommandTaskSpec, ...]:
    """Prepare, bake and natively build every AMD64 cell.

    Mirrors `arm64_build_commands`: the builder itself is acquired by a
    resource, so nothing here creates or bootstraps it.
    """
    commands: list[CommandTaskSpec] = []
    seen: set[str] = set()
    for cell in plan.bake_cells:
        prerequisite = cell.prerequisite_command
        if prerequisite is None or cell.target.name in seen:
            continue
        seen.add(cell.target.name)
        commands.append(
            CommandTaskSpec(
                task_id=f"release.images.prepare.{cell.target.name}",
                summary=f"Prepare {cell.target.name} JVM image",
                argv=prerequisite,
                role="stack",
                remote_dir=remote_source_dir,
            )
        )
    commands.append(
        CommandTaskSpec(
            task_id="release.images.bake.amd64",
            summary="Build AMD64 Dockerfile images",
            argv=(
                "docker",
                "buildx",
                "bake",
                "--builder",
                builder_name,
                "--file",
                remote_bake_file,
                "--load",
                "docker-amd64",
            ),
            role="stack",
            remote_dir=remote_source_dir,
        )
    )
    commands.extend(
        CommandTaskSpec(
            task_id=f"release.images.native.{cell.target.name}",
            summary=f"Build {cell.target.name} AMD64 native image",
            argv=cell.gradle_command or (),
            role="stack",
            remote_dir=remote_source_dir,
        )
        for cell in plan.gradle_cells
    )
    return tuple(commands)
```

Aggiungere `from nanolab.images.plan import ImagePlan` agli import se non è già importato a runtime (oggi `Amd64ReleasePlan` è sotto `TYPE_CHECKING` a riga 22).

- [ ] **Step 4: Aggiornare l'unico chiamante**

In `release/build.py:496-526`, `_build_amd64_images` passa oggi `plan` intero. Il runner legacy crea ancora il builder da sé, quindi va reintrodotto lì lo stesso paio di comandi che abbiamo tolto — il runner muore nel Task 11 e questa è la sua ultima settimana di vita:

```python
    builder_commands = (
        CommandTaskSpec(
            task_id="release.buildx.create",
            summary="Create bounded release Buildx builder",
            argv=(
                "docker", "buildx", "create",
                "--name", plan.builder.name,
                "--driver", "docker-container",
                "--buildkitd-config", remote_buildkit,
                "--use",
            ),
            role="stack",
            remote_dir=remote_source_dir,
        ),
        CommandTaskSpec(
            task_id="release.buildx.bootstrap",
            summary="Bootstrap bounded release Buildx builder",
            argv=("docker", "buildx", "inspect", "--builder", plan.builder.name, "--bootstrap"),
            role="stack",
            remote_dir=remote_source_dir,
        ),
    )
    for command in (
        *builder_commands,
        *amd64_build_commands(
            plan.image_plan,
            builder_name=plan.builder.name,
            remote_bake_file=remote_bake,
            remote_source_dir=remote_source_dir,
        ),
    ):
```

(adattare i nomi delle variabili locali `remote_bake` / `remote_buildkit` / `remote_source_dir` a quelli già presenti alle righe 497-521).

- [ ] **Step 5: Eseguire i test e verificare che passino**

```bash
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/release -v
```

Atteso: verde, incluso `test_run_amd64.py` che esercita il runner legacy.

- [ ] **Step 6: Commit**

```bash
git add packages/nanolab/src/nanolab/release/build.py packages/nanolab/tests/release/test_build.py
git commit -m "refactor: give amd64_build_commands the arm64 shape"
```

---

## Task 4: Costruire AMD64 con i comandi veri — bake, prerequisiti JVM, gradle completo, BuildKit

**Questo è il difetto centrale.** `amd64_build_composite` (`release_composites.py:122`) fa `docker build` per cella e `GradleTask(target, properties={prop: cell.image})`. Rispetto a `cell.gradle_command` (`images/plan.py:46-68`) **perde `-PimagePlatform=linux/amd64` e `-PcontrolPlaneModules=all`**: il control-plane native AMD64 di v0.18.1 è stato buildato con i moduli di default, non con `all`. Inoltre non esegue mai `prerequisite_command` (`images/plan.py:71`), quindi le immagini JVM AMD64 si costruiscono su qualunque jar fosse rimasto nella working tree.

Aggravante: `plans/release.py:349` registra `cell.gradle_command` in `phase_inputs`, cioè nella reuse key — il receipt dichiara di dipendere da argomenti che il comando eseguito non contiene.

Terzo difetto nello stesso punto: `buildx_builder_resource` è chiamato senza `buildkitd_config` (`plans/release.py:325-330`), quindi `build.maxParallelism: 4` di `packages/nanolab/release.yaml` è inerte sul lato AMD64, pur essendo anch'esso registrato in `phase_inputs` (`:353`).

**Files:**
- Modify: `packages/nanolab/src/nanolab/release/resources.py:418-481` (generalizzare `arm_build_inputs_resource`)
- Modify: `packages/nanolab/src/nanolab/release/tasks.py:87-97` (la verifica della matrice deve riconoscere anche i digest locali)
- Modify: `packages/nanolab/src/nanolab/plans/release.py:324-364`
- Modify: `packages/nanolab/src/nanolab/plans/release.py:19-23` (import), `:453-500` (chiamata ARM)
- Delete: `packages/sonata-tasks/src/sonata_tasks/release_composites.py:117-186` (`amd64_build_composite`)
- Test: `packages/nanolab/tests/plans/test_release.py` (nuovo), `packages/nanolab/tests/release/test_tasks.py`, `packages/nanolab/tests/release/test_resources.py`

**Interfaces:**
- Consumes: `command_specs_composite` (Task 2), `amd64_build_commands(plan, builder_name=…, remote_bake_file=…, remote_source_dir=…)` (Task 3)
- Produces:
  ```python
  def build_inputs_resource(
      *,
      image_plan: ImagePlan,
      max_parallelism: int,
      run_dir: Path,
      remote_root: str,
      provider: object,
      request: object,
      architecture: str,          # "amd64" | "arm64" — entra nel nome del bake file
      requires: tuple[Resource[Any], ...] = (),
  ) -> Resource[BuildInputs]
  ```
  `BuildInputs` è l'attuale `ArmBuildInputs` rinominata, stessi quattro campi (`bake`, `buildkit`, `remote_bake`, `remote_buildkit`).

- [ ] **Step 1: Scrivere il test che fallisce sulla composizione dei comandi AMD64**

In `packages/nanolab/tests/plans/test_release.py`:

**Il difetto vive nel cablaggio, non nel generatore.** `amd64_build_commands` è già corretto oggi: `plans/release.py` semplicemente non lo chiama. Il test deve quindi guardare la fase compilata, non i comandi. `ReleasePhaseTask.phase_inputs` è un campo pubblico della dataclass (`release/tasks.py:45`) e il Task 4 lo fa derivare dagli argv reali, quindi è il punto di osservazione giusto.

```python
def test_amd64_build_phase_records_the_commands_it_will_run(release_request) -> None:
    workflow = build_release_workflow(release_request, provider=_FakeProvider())
    phase = _phase_named(workflow, "build-amd64-images")

    argvs = [argv for argv, _role, _remote_dir in phase.phase_inputs["commands"]]

    natives = [a for a in argvs if a[0] == "./gradlew" and "-PimagePlatform=linux/amd64" in a]
    assert natives, "no native AMD64 gradle build"
    assert all("-PcontrolPlaneModules=all" in argv for argv in natives)
    assert any(argv[:3] == ("docker", "buildx", "bake") for argv in argvs)
    assert any(argv[0] == "./gradlew" and "bootJar" in " ".join(argv) for argv in argvs)
```

`release_request` è una fixture che costruisce un `ReleaseRequest` con `build_release_request` su un repo temporaneo; `_phase_named` cerca in `workflow` il task il cui `.phase` corrisponde; `_FakeProvider` è il doppio di provider già usato in `packages/nanolab/tests/release/_release_support.py`. Se il file `packages/nanolab/tests/plans/test_release.py` non esiste, crearlo seguendo lo stile di `packages/nanolab/tests/plans/test_loadtest.py`.

- [ ] **Step 2: Eseguire il test e verificare che fallisca**

```bash
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml \
  packages/nanolab/tests/plans/test_release.py::test_amd64_build_phase_records_the_commands_it_will_run -v
```

Atteso: `KeyError: 'commands'` — oggi la fase registra `phase_inputs["cells"]`, cioè una descrizione delle celle che non corrisponde ai comandi eseguiti.

- [ ] **Step 3: Generalizzare il resource di staging degli input di build**

In `release/resources.py`, rinominare `ArmBuildInputs` → `BuildInputs` e `arm_build_inputs_resource` → `build_inputs_resource`, aggiungendo il parametro `architecture`:

```python
def build_inputs_resource(
    *,
    image_plan: ImagePlan,
    max_parallelism: int,
    run_dir: Path,
    remote_root: str,
    provider: object,
    request: object,
    architecture: str,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[BuildInputs]:
    """Stage the two generated inputs consumed by a Buildx builder."""
    remote_root = str(_release_remote_root(remote_root))
    bake = Path(run_dir) / f"docker-bake-{architecture}.json"
    buildkit = Path(run_dir) / f"buildkitd-{architecture}.toml"
```

Il resto del corpo (righe 433-480) resta identico; aggiornare solo il `title` del `Resource`:

```python
        title=f"Acquire {architecture.upper()} Bake and BuildKit inputs",
```

Il nome del file buildkitd diventa per-architettura perché stack e arm-builder scrivono ora nella **stessa** `run_dir` locale: senza il suffisso si sovrascriverebbero a vicenda, e la `cleanup()` dell'uno cancellerebbe il file dell'altro.

- [ ] **Step 4: Aggiornare la chiamata ARM esistente**

In `plans/release.py`, alla chiamata attuale (`:490` circa) aggiungere `architecture="arm64"` e rinominare l'import da `arm_build_inputs_resource` a `build_inputs_resource`. Nei due punti che referenziano il path remoto del buildkitd ARM (`:478` e `:515`) sostituire `f"{remote_root}/buildkitd.toml"` con il valore dal resource: `arm_inputs` è già una `Resource`, quindi usare `f"{remote_root}/buildkitd-arm64.toml"` mantenendo la coerenza con lo Step 3.

Analogamente per il bake ARM (`:495`, `:518-519`): `docker-bake-arm64.json` non cambia nome, era già così.

- [ ] **Step 5: Far riconoscere alla verifica della matrice anche i digest locali**

`ReleasePhaseTask.run` controlla `expected_images` (`release/tasks.py:87-97`) contando **solo** le evidenze di tipo `local-registry-digest` con riferimento `docker://<image>`. La fase `amd64-build` gira con `registry=False` e produce `local-image-digest` con riferimento `docker-daemon:<image>`: dichiarare `expected_images` senza toccare questo controllo lo farebbe fallire **sempre**, perché troverebbe zero evidenze corrispondenti.

In `release/tasks.py`, sostituire il blocco 87-97:

```python
        if self.expected_images:
            # Both digest kinds cover the same matrix: the build phase inspects
            # the daemon (docker-daemon:), the push phase inspects the registry
            # (docker://). Compare the image, not the scheme it was read through.
            matrix = tuple(
                entry
                for entry in produced
                if entry.kind in ("local-registry-digest", "local-image-digest")
            )
            references = {
                entry.reference.removeprefix("docker://").removeprefix("docker-daemon:")
                for entry in matrix
            }
            if (
                len(matrix) != len(self.expected_images)
                or references != set(self.expected_images)
                or any(not is_sha256_digest(entry.digest) for entry in matrix)
            ):
                raise RuntimeError(f"{self.phase} evidence does not cover the image matrix")
```

Il messaggio d'errore era hardcodato a `"local-registry-push"` anche quando a fallire era un'altra fase; ora nomina la fase vera.

Aggiungere in `packages/nanolab/tests/release/test_tasks.py`:

```python
def test_expected_images_accepts_daemon_local_digests() -> None:
    digest = "sha256:" + "c" * 64
    task = _phase_task(
        expected_images=("localhost:5000/nanofaas/server:v1-amd64",),
        work=lambda _inputs: (
            Evidence("local-image-digest", "docker-daemon:localhost:5000/nanofaas/server:v1-amd64", digest),
        ),
    )

    outcome = task.run(_inputs())

    assert any(item.kind == "local-image-digest" for item in outcome.evidence)


def test_expected_images_still_rejects_an_incomplete_matrix() -> None:
    task = _phase_task(
        expected_images=("a:v1", "b:v1"),
        work=lambda _inputs: (
            Evidence("local-image-digest", "docker-daemon:a:v1", "sha256:" + "d" * 64),
        ),
    )

    with pytest.raises(RuntimeError, match="does not cover the image matrix"):
        task.run(_inputs())
```

`_phase_task` costruisce un `ReleasePhaseTask` con `identity`/`run_dir` fittizi su `tmp_path`.

- [ ] **Step 6: Cablare la fase AMD64 sui comandi veri**

In `plans/release.py`, sostituire il blocco `# --- Phase 2: AMD64 Build ---` (righe 324-364):

```python
    # --- Phase 2: AMD64 Build ---
    amd64_inputs = build_inputs_resource(
        image_plan=request.image_plan,
        max_parallelism=request.settings.max_parallelism,
        run_dir=release_dir,
        remote_root=remote_root,
        provider=provider,
        request=stack_req,
        architecture="amd64",
        requires=(infrastructure.stack,),
    )
    amd64_builder_name = f"release-amd64-{request.version}"
    amd64_builder = buildx_builder_resource(
        name=amd64_builder_name,
        executor=executor,
        role="stack",
        requires=(infrastructure.stack, amd64_inputs),
        buildkitd_config=f"{remote_root}/buildkitd-amd64.toml",
    )
    amd64_commands = amd64_build_commands(
        request.image_plan,
        builder_name=amd64_builder_name,
        remote_bake_file=f"{remote_root}/docker-bake-amd64.json",
        remote_source_dir=source_dir,
    )
    amd64_steps = command_specs_composite(
        amd64_commands, executor=executor, title="Build AMD64 images"
    )
    release_images = tuple(cell.image for cell in request.image_plan.cells)
    amd64_build = amd64_build_task(
        identity=identity,
        run_dir=request.run_dir,
        phase_inputs={
            "commands": tuple(
                (command.argv, command.role, str(command.remote_dir))
                for command in amd64_commands
            ),
            "maxParallelism": request.settings.max_parallelism,
            "sourceDir": source_dir,
        },
        prerequisites=(source_tests.receipt,),
        expected_images=release_images,
        work=lambda inputs: run_image_steps(
            amd64_steps,
            inputs,
            executor,
            release_images,
            registry=False,
        ),
    )
```

`expected_images=release_images` è il punto A5: `registry_push` lo dichiarava (`:379`) e `arm64_build` lo dichiarava (`:506`), la build AMD64 no. Il Task 5 aggiunge la verifica d'architettura a questa stessa chiamata.

`phase_inputs["commands"]` ora deriva dagli argv realmente eseguiti, quindi la reuse key non può più divergere dal comando: era il bug latente di `plans/release.py:349`.

Spostare la definizione di `release_images` qui e rimuoverla dalla sua posizione attuale (`:373`), lasciando `registry_push` a usarla.

Aggiornare gli import in cima: `command_specs_composite` e `registry_push_composite` da `sonata_tasks.release_composites`, `amd64_build_commands` da `nanolab.release.build`.

- [ ] **Step 7: Aggiungere il resource alla catena `requires` del DAG**

In `plans/release.py:799-847`, la riga che aggiunge `amd64_build` diventa:

```python
wf.add(amd64_build, requires=(infrastructure.stack, sources.stack, amd64_inputs, amd64_builder))
```

- [ ] **Step 8: Cancellare `amd64_build_composite`**

Rimuovere `release_composites.py:117-186`, la voce in `__all__`, il re-export in `sonata_tasks/__init__.py`, e i test che lo esercitano in `test_release_composites.py`. Rimuovere anche gli import ora inutilizzati (`DockerBuildTask`, `GradleTask`) se nessun altro composite del file li usa.

- [ ] **Step 9: Eseguire tutte le suite**

```bash
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests
uv run --locked --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
uv run --locked --all-packages --all-groups lint-imports --config packages/sonata-tasks/.importlinter --no-cache
```

Atteso: tutto verde. Il test dello Step 1 ora passa.

- [ ] **Step 10: Verificare la compilazione del DAG senza chiamate cloud**

```bash
cd packages/nanolab && NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run nanolab plan \
  scenarios-v2/release.yaml --environment environments/azure-release.yaml
```

Atteso: 15 fasi elencate, `build-amd64-images` ancora presente, nessun errore.

- [ ] **Step 11: Commit**

```bash
git add packages/nanolab packages/sonata-tasks
git commit -m "fix: build AMD64 with the whole build command, not a subset of it"
```

---

## Task 5: Verificare l'architettura delle immagini AMD64 e dichiararne la matrice

`build.py:679 _require_image_architecture` esiste e il ramo ARM la chiama per ogni cella (`build.py:590`); AMD64 mai. Un'immagine con architettura sbagliata passa `docker image inspect --format={{.Id}}` (`release/tasks.py:285`) senza obiezioni e arriva fino al manifest list.

Stesso task: `amd64_build_task` non dichiarava `expected_images` (il Task 4 l'ha già aggiunto) — qui si verifica che serva davvero, e si documenta l'assenza deliberata di uno smoke AMD64.

**Files:**
- Modify: `packages/nanolab/src/nanolab/release/tasks.py:264-305` (`run_image_steps`)
- Modify: `packages/nanolab/src/nanolab/plans/release.py` — la `work=` di `amd64_build_task` scritta dal Task 4, più il commento sullo smoke
- Test: `packages/nanolab/tests/release/test_tasks.py`

**Interfaces:**
- Consumes: `run_image_steps` come lasciata dal Task 4
- Produces:
  ```python
  def run_image_steps(
      steps: Task[Any],
      inputs: TaskInputs,
      executor: CommandTaskExecutor,
      images: tuple[str, ...],
      *,
      registry: bool,
      architecture: str | None = None,
  ) -> tuple[Evidence, ...]
  ```
  `architecture=None` (il default, usato da `registry_push`) salta la verifica.

- [ ] **Step 1: Scrivere il test che fallisce**

In `packages/nanolab/tests/release/test_tasks.py`:

```python
def test_run_image_steps_rejects_a_foreign_architecture() -> None:
    executor = _ScriptedExecutor(
        {
            ("docker", "image", "inspect", "--format={{.Architecture}}", "img:v1"): "arm64",
            ("docker", "image", "inspect", "--format={{.Id}}", "img:v1"): "sha256:" + "a" * 64,
        }
    )

    with pytest.raises(RuntimeError, match="image architecture mismatch"):
        run_image_steps(
            _NoopSteps(), _inputs(), executor, ("img:v1",),
            registry=False, architecture="amd64",
        )


def test_run_image_steps_accepts_the_expected_architecture() -> None:
    digest = "sha256:" + "b" * 64
    executor = _ScriptedExecutor(
        {
            ("docker", "image", "inspect", "--format={{.Architecture}}", "img:v1"): "amd64",
            ("docker", "image", "inspect", "--format={{.Id}}", "img:v1"): digest,
        }
    )

    evidence = run_image_steps(
        _NoopSteps(), _inputs(), executor, ("img:v1",),
        registry=False, architecture="amd64",
    )

    assert [item.digest for item in evidence] == [digest]
```

`_ScriptedExecutor` mappa argv→stdout e restituisce `TaskResult(status="passed", return_code=0, stdout=…)`; `_NoopSteps` è un `Task` il cui `run` restituisce un `TaskOutcome` vuoto. Se `test_tasks.py` ha già doppi equivalenti, riusarli invece di aggiungerne.

- [ ] **Step 2: Eseguire i test e verificare che falliscano**

```bash
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/release/test_tasks.py -v -k architecture
```

Atteso: `TypeError: run_image_steps() got an unexpected keyword argument 'architecture'`.

- [ ] **Step 3: Implementare la verifica**

In `release/tasks.py`, aggiungere il parametro e il controllo prima della raccolta del digest:

```python
def run_image_steps(
    steps: Task[Any],
    inputs: TaskInputs,
    executor: CommandTaskExecutor,
    images: tuple[str, ...],
    *,
    registry: bool,
    architecture: str | None = None,
) -> tuple[Evidence, ...]:
    """Run build/push steps and capture the complete current image matrix.

    `architecture`, when given, asserts every image really carries it: a
    cross-built or mistagged image otherwise inspects cleanly and reaches the
    manifest list, where the mismatch surfaces as a runtime failure on a user's
    machine instead of here.
    """
    _run_steps(steps, inputs)
    evidence: list[Evidence] = []
    for image in images:
        if architecture is not None:
            _require_architecture(executor, image, architecture)
        argv = (
            ...
```

E in fondo al modulo:

```python
def _require_architecture(
    executor: CommandTaskExecutor, image: str, expected: str
) -> None:
    result = executor.run(
        CommandTaskSpec(
            task_id="",
            summary=f"Verify {image} architecture",
            argv=("docker", "image", "inspect", "--format={{.Architecture}}", image),
            role="stack",
        )
    )
    actual = result.stdout.strip() if isinstance(result.stdout, str) else ""
    if result.status != "passed" or actual != expected:
        raise RuntimeError(
            f"image architecture mismatch for {image}: expected {expected}, got {actual or 'empty'}"
        )
```

- [ ] **Step 4: Attivare la verifica sulla fase AMD64**

In `plans/release.py`, nella `work=` di `amd64_build_task` scritta dal Task 4, aggiungere il keyword:

```python
        work=lambda inputs: run_image_steps(
            amd64_steps,
            inputs,
            executor,
            release_images,
            registry=False,
            architecture="amd64",
        ),
```

`registry_push` resta senza `architecture`: le stesse immagini sono già state verificate qui, e sul ramo registry `skopeo inspect --format={{.Digest}}` non riporta l'architettura.

- [ ] **Step 5: Eseguire i test e verificare che passino**

```bash
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/release -v
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/plans -v
```

Atteso: verde.

- [ ] **Step 6: Documentare l'assenza deliberata di uno smoke AMD64**

L'asimmetria con `arm64-smoke` è difendibile — le tre fasi di benchmark girano contro le immagini AMD64 pushate nel registry locale e falliscono se non partono, quindi lo smoke AMD64 è già coperto dal loadtest, mentre ARM64 non ha nessun carico che lo eserciti. Va scritto, non lasciato dedurre.

In `plans/release.py`, sopra `wf.add(arm64_smoke, …)`:

```python
    # ARM64 gets a smoke phase and AMD64 does not, on purpose: the three
    # benchmark runs exercise every AMD64 image from the local registry and fail
    # if one does not start, so AMD64 is smoke-tested by the loadtest. Nothing
    # runs the ARM64 images otherwise, so they need their own.
```

- [ ] **Step 7: Commit**

```bash
git add packages/nanolab
git commit -m "fix: assert AMD64 images are actually AMD64"
```

---

## Task 6: Dare a `source-tests` un'evidenza, così il resume può saltarla

`receipts/source-tests.json` del canary 0.18.1 contiene `"evidence": []`. Per il design della migrazione (`docs/plans/2026-08-01-sonata-release-migration-design.md`, *"no evidence means no reuse"*), la fase si rifà interamente a ogni `--resume`: l'intera suite di test del prodotto gira di nuovo anche quando è già passata sullo stesso albero sorgente.

L'evidenza corretta è il digest dell'albero testato: se il sorgente è lo stesso, il risultato dei test è lo stesso.

**Files:**
- Modify: `packages/nanolab/src/nanolab/release/tasks.py:259-261` (`run_source_steps`)
- Modify: `packages/nanolab/src/nanolab/plans/release.py:306-322`
- Test: `packages/nanolab/tests/release/test_tasks.py`

**Interfaces:**
- Consumes: `digest_path` da `nanolab.release.state` (dal Task 11: da `nanolab.release.model`)
- Produces: `run_source_steps(steps: Task[Any], inputs: TaskInputs, *, source_archive: Path) -> tuple[Evidence, ...]` — restituisce una `Evidence("file-digest", str(source_archive), digest_path(source_archive))`.

- [ ] **Step 1: Scrivere il test che fallisce**

```python
def test_run_source_steps_records_the_tested_source_tree(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar"
    archive.write_bytes(b"tree")

    evidence = run_source_steps(_NoopSteps(), _inputs(), source_archive=archive)

    assert len(evidence) == 1
    assert evidence[0].kind == "file-digest"
    assert evidence[0].reference == str(archive)
    assert evidence[0].digest == digest_path(archive)
```

- [ ] **Step 2: Eseguire il test e verificare che fallisca**

```bash
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml \
  packages/nanolab/tests/release/test_tasks.py::test_run_source_steps_records_the_tested_source_tree -v
```

Atteso: `TypeError: run_source_steps() got an unexpected keyword argument 'source_archive'`.

- [ ] **Step 3: Implementare**

In `release/tasks.py`:

```python
def run_source_steps(
    steps: Task[Any], inputs: TaskInputs, *, source_archive: Path
) -> tuple[Evidence, ...]:
    """Run the source tests and record which tree they ran against.

    The archive digest is the whole evidence a resume needs: same tree, same
    test outcome. Returning nothing here means the phase can never be reused
    and every resume re-runs the full product suite.
    """
    _run_steps(steps, inputs)
    return (Evidence("file-digest", str(source_archive), digest_path(source_archive)),)
```

- [ ] **Step 4: Aggiornare il chiamante**

In `plans/release.py:321`, la `work=` di `source_test_task` diventa:

```python
        work=lambda inputs: run_source_steps(
            source_steps, inputs, source_archive=release_dir / "source.tar"
        ),
```

Verificare che `release_dir / "source.tar"` sia il path locale realmente prodotto da `build_release_source_resources` (`release/resources.py:352`); se il nome differisce, usare quello e non inventarlo.

- [ ] **Step 5: Eseguire i test e verificare che passino**

```bash
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/release -v
```

Atteso: verde.

- [ ] **Step 6: Commit**

```bash
git add packages/nanolab
git commit -m "fix: let a resumed release skip source tests it already passed"
```

---

## Task 7: Correggere i flag di `CosignTask`

`CosignTask` (`sonata-tasks/src/sonata_tasks/cosign.py:86-102`) genera comandi cosign che **non corrispondono** a quelli dell'implementazione procedurale in produzione (`release/attest.py:113-176`). Mancano quattro cose:

| Operazione | `CosignTask` oggi | `_cosign` in produzione |
|---|---|---|
| `sign` | `sign --key /key.cosign <img>` | `sign --yes --key … <img>` |
| `attest` | `attest --key … --predicate … <img>` | `attest --yes --key … --type custom --predicate … <img>` |
| `attach sbom` | `attach sbom --sbom … <img>` | `attach sbom --sbom … --type spdx <img>` |
| `verify-attestation` | `verify-attestation --key … <img>` | `verify-attestation --key … --type custom <img>` |

Senza `--yes` cosign chiede conferma interattiva e la fase si pianta; senza `--type custom` l'attestazione ha il predicate type sbagliato e `verify-attestation` non la trova. Vanno corretti **prima** di cablare il composite (Task 9), altrimenti si sostituisce codice funzionante con codice rotto.

Serve anche l'operazione `public-key`, oggi assente: `cosign verify` rifiuta la chiave privata (*"unknown Public key PEM file type: ENCRYPTED SIGSTORE PRIVATE KEY"*, vedi il commento a `attest.py:155-157`), quindi la metà pubblica va derivata prima di verificare.

**Files:**
- Modify: `packages/sonata-tasks/src/sonata_tasks/cosign.py:18-107`
- Test: `packages/sonata-tasks/tests/test_cosign.py`

**Interfaces:**
- Produces: `CosignOperation` estesa con `"public-key"`; `CosignTask` con un nuovo parametro `output_file: str | None = None` usato solo da `public-key`.

- [ ] **Step 1: Scrivere i test che falliscono**

In `packages/sonata-tasks/tests/test_cosign.py`:

```python
def test_sign_does_not_wait_for_confirmation() -> None:
    executor = RecordingExecutor()
    _run(CosignTask(
        operation="sign", image="img@sha256:aa", key_file="/secrets/cosign.key",
        password_file="/secrets/pw", docker_config="/home/user/.docker",
        executor=executor, role="stack",
    ))
    argv = executor.seen[0].argv
    assert argv[-4:] == ("sign", "--yes", "--key", "/key.cosign") or "--yes" in argv


def test_attest_declares_the_custom_predicate_type() -> None:
    executor = RecordingExecutor()
    _run(CosignTask(
        operation="attest", image="img@sha256:aa", key_file="/secrets/cosign.key",
        password_file="/secrets/pw", docker_config="/home/user/.docker",
        predicate_file="/work/predicate.json", executor=executor, role="stack",
    ))
    argv = executor.seen[0].argv
    assert "--yes" in argv
    assert argv[argv.index("--type") + 1] == "custom"


def test_attach_sbom_declares_spdx() -> None:
    executor = RecordingExecutor()
    _run(CosignTask(
        operation="attach sbom", image="img@sha256:aa", key_file="/secrets/cosign.key",
        password_file="/secrets/pw", docker_config="/home/user/.docker",
        sbom_file="/work/sbom.spdx.json", executor=executor, role="stack",
    ))
    argv = executor.seen[0].argv
    assert argv[argv.index("--type") + 1] == "spdx"


def test_verify_attestation_declares_the_custom_predicate_type() -> None:
    executor = RecordingExecutor()
    _run(CosignTask(
        operation="verify-attestation", image="img@sha256:aa",
        key_file="/secrets/cosign.key", password_file="/secrets/pw",
        docker_config="/home/user/.docker", public_key_file="/work/cosign.pub",
        executor=executor, role="stack",
    ))
    argv = executor.seen[0].argv
    assert argv[argv.index("--type") + 1] == "custom"


def test_public_key_writes_the_derived_key_to_a_file() -> None:
    executor = RecordingExecutor()
    _run(CosignTask(
        operation="public-key", image="", key_file="/secrets/cosign.key",
        password_file="/secrets/pw", docker_config="/home/user/.docker",
        output_file="/work/cosign.pub", executor=executor, role="stack",
    ))
    argv = executor.seen[0].argv
    assert "public-key" in argv
    assert "/work/cosign.pub" in " ".join(argv)
```

`_run` è l'helper già presente nel file che esegue il task con un `TaskInputs` fittizio; se non esiste, chiamare `task.run(TaskInputs(...))` come fanno i test attuali.

- [ ] **Step 2: Eseguire i test e verificare che falliscano**

```bash
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/test_cosign.py -v
```

Atteso: cinque FAIL.

- [ ] **Step 3: Correggere i comandi**

In `cosign.py`, sostituire il blocco 84-107:

```python
        # Build the cosign subcommand run inside the container.
        # --yes: these run unattended; without it cosign blocks on a prompt.
        # --type: the release predicate is `custom`, and verify-attestation only
        # finds an attestation whose type it was told to expect.
        cosign: tuple[str, ...]
        if operation == "sign":
            cosign = ("sign", "--yes", "--key", "/key.cosign", image)
        elif operation == "attest":
            cosign = (
                "attest",
                "--yes",
                "--key",
                "/key.cosign",
                "--type",
                "custom",
                "--predicate",
                "/predicate.json",
                image,
            )
        elif operation == "attach sbom":
            cosign = ("attach", "sbom", "--sbom", "/sbom.json", "--type", "spdx", image)
        elif operation == "verify":
            cosign = ("verify", "--key", "/pub.key", image)
        elif operation == "verify-attestation":
            cosign = ("verify-attestation", "--key", "/pub.key", "--type", "custom", image)
        elif operation == "public-key":
            if output_file is None:
                raise ValueError("cosign public-key needs an output_file")
            cosign = ("public-key", "--key", "/key.cosign")
        else:
            raise ValueError(f"unknown cosign operation: {operation}")
```

Aggiornare `CosignOperation` (riga 18) e la firma:

```python
CosignOperation = Literal[
    "sign", "attest", "attach sbom", "verify", "verify-attestation", "public-key"
]
```

```python
        output_file: str | None = None,
```

`public-key` monta la chiave privata come `sign`/`attest`, quindi estendere la condizione a riga 75:

```python
        if operation in ("sign", "attest", "public-key"):
            run.extend(["-v", f"{key_file}:/key.cosign:ro"])
```

E, poiché `cosign public-key` scrive su stdout, redirigere nel wrapper. Sostituire la costruzione dell'argv finale (righe 109-127):

```python
        redirect = f' > "{output_file}"' if operation == "public-key" else ""
        super().__init__(
            title=title or f"cosign {operation} {image or key_file}",
            # Password is read from a file by the shell wrapper and passed via
            # environment variable -- never appears in the process argv.
            argv=(
                "sh",
                "-c",
                # ponytail: shell wrapper reads password file, shifts it away,
                # then execs the real command with the password in the env.
                # public-key writes to stdout, so it redirects instead of exec'ing.
                (
                    'pw=$(cat "$1"); shift; COSIGN_PASSWORD="$pw" exec "$@"'
                    if not redirect
                    else f'pw=$(cat "$1"); shift; COSIGN_PASSWORD="$pw" "$@"{redirect}'
                ),
                "--",
                password_file,
                *run,
            ),
            executor=executor,
            role=role,
            cwd=cwd,
            verify=verify,
        )
```

- [ ] **Step 4: Eseguire i test e verificare che passino**

```bash
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/test_cosign.py -v
```

Atteso: verde, inclusi i test preesistenti (aggiornare quelli che asserivano l'argv vecchio — l'assenza di `--yes` era il bug, non il contratto).

- [ ] **Step 5: Commit**

```bash
git add packages/sonata-tasks/src/sonata_tasks/cosign.py packages/sonata-tasks/tests/test_cosign.py
git commit -m "fix: make CosignTask issue the commands the release actually needs"
```

---

## Task 8: Completare `attest_composite` a cinque operazioni con evidenza per immagine

`attest_composite` (`release_composites.py:602`) fa solo syft + attest. L'implementazione in produzione (`release/attest.py:69-176`) fa cinque cose per immagine: syft SBOM, `cosign sign`, `cosign attest`, `cosign attach sbom`, e poi `cosign verify` + `verify-attestation`.

Questa è **l'unica fase dove i composite pagano davvero**: su ~30 digest unici sono ~150 invocazioni `docker run` dentro un singolo task opaco, e un fallimento di rete alla novantesima rifà tutto al `--resume`. `Steps` è journalizzato per singolo step (`sonata_engine/core/steps.py:12`) e annida (il pattern è già in `registry_push_composite:227-247`).

Le altre fasi non lo giustificano: `publish-aliases` sono 9 comandi, `arm64-build` è un unico bake batch (lo dice il suo stesso commento `ponytail:` a `release_composites.py:291`).

**Files:**
- Modify: `packages/sonata-tasks/src/sonata_tasks/release_composites.py:597-677`
- Test: `packages/sonata-tasks/tests/test_release_composites.py`

**Interfaces:**
- Consumes: `SyftTask`, `CosignTask` (con i flag del Task 7)
- Produces:
  ```python
  def attest_composite(
      images: Sequence[str],
      *,
      predicate_remote: str,
      sbom_dir_remote: str,
      public_key_remote: str,
      cosign_key: str,
      password_file: str,
      docker_config: str,
      executor: CommandTaskExecutor,
      role: ExecutionRole,
      title: str = "Attest images",
  ) -> Steps
  ```
  `images` sono **riferimenti pinnati per digest** (`repo/name@sha256:…`), non tag. Il chiamante (Task 9) fa il pinning. Tutti i path sono `str` remoti, non `Path` locali — era un errore di tipo nella firma attuale (`predicate_remote: Path`).

- [ ] **Step 1: Scrivere il test che fallisce**

```python
def test_attest_composite_runs_five_operations_per_image() -> None:
    executor = RecordingExecutor()

    composite = attest_composite(
        ("repo/a@sha256:aa", "repo/b@sha256:bb"),
        predicate_remote="/work/predicate.json",
        sbom_dir_remote="/work/sboms",
        public_key_remote="/work/cosign.pub",
        cosign_key="/secrets/cosign-key",
        password_file="/secrets/cosign-password",
        docker_config="/home/azureuser/.docker",
        executor=executor,
        role="stack",
    )

    # Un Steps per immagine, così un resume riprende dall'immagine non firmata.
    assert len(composite.steps) == 2
    for per_image in composite.steps:
        titles = [step.title for step in per_image.steps]
        assert len(titles) == 5
        assert sum("Syft" in title for title in titles) == 1
        assert sum("cosign" in title for title in titles) == 4


def test_attest_composite_pins_every_operation_to_the_same_digest() -> None:
    executor = RecordingExecutor()
    composite = attest_composite(
        ("repo/a@sha256:aa",),
        predicate_remote="/work/predicate.json",
        sbom_dir_remote="/work/sboms",
        public_key_remote="/work/cosign.pub",
        cosign_key="/secrets/cosign-key",
        password_file="/secrets/cosign-password",
        docker_config="/home/azureuser/.docker",
        executor=executor,
        role="stack",
    )
    _run_composite(composite, executor)

    assert executor.seen, "composite ran nothing"
    for spec in executor.seen:
        assert "repo/a@sha256:aa" in spec.argv, f"unpinned reference in {spec.argv}"


def test_attest_composite_handles_an_empty_image_set() -> None:
    executor = RecordingExecutor()
    composite = attest_composite(
        (),
        predicate_remote="/work/predicate.json",
        sbom_dir_remote="/work/sboms",
        public_key_remote="/work/cosign.pub",
        cosign_key="/secrets/cosign-key",
        password_file="/secrets/cosign-password",
        docker_config="/home/azureuser/.docker",
        executor=executor,
        role="stack",
    )

    assert len(composite.steps) == 1
```

Semplificare la prima asserzione se l'introspezione dei titoli risulta fragile: ciò che conta è **cinque step per immagine** e **ogni argv pinnato al digest**. `_run_composite` esegue lo `Steps` con un `TaskInputs` fittizio come già fanno gli altri test del file.

- [ ] **Step 2: Eseguire i test e verificare che falliscano**

```bash
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/sonata-tasks/pyproject.toml \
  packages/sonata-tasks/tests/test_release_composites.py -v -k attest
```

Atteso: FAIL — la firma attuale non accetta `public_key_remote` né `password_file` come keyword, e produce 2 step per immagine.

- [ ] **Step 3: Riscrivere il composite**

Sostituire `release_composites.py:597-677`:

```python
# ---------------------------------------------------------------------------
# attest_composite
# ---------------------------------------------------------------------------


def attest_composite(
    images: Sequence[str],
    *,
    predicate_remote: str,
    sbom_dir_remote: str,
    public_key_remote: str,
    cosign_key: str,
    password_file: str,
    docker_config: str,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    title: str = "Attest images",
) -> Steps:
    """SBOM, sign, attest, attach and verify every digest, one Steps per image.

    The per-image grouping is the point: this phase issues five container runs
    per digest across the whole published matrix, and a network failure two
    thirds of the way through should resume from the digest it died on, not
    from the first one.

    `images` must be digest-pinned references (``repo/name@sha256:...``).
    Signing a tag signs whatever the tag points at when cosign resolves it,
    which is not necessarily what the release verified.

    `public_key_remote` must already hold the public half of `cosign_key`:
    ``cosign verify`` rejects an encrypted private key outright. Deriving it is
    a one-shot setup step, so it belongs to the caller, not to this per-image
    composite.
    """
    if not images:
        return Steps(
            title=title,
            steps=(
                CommandTask(
                    title="No images to attest", argv=("true",), executor=executor, role=role
                ),
            ),
        )

    cell_steps: list[Any] = []
    for image in images:
        sbom_path = f"{sbom_dir_remote}/{_artifact_slug(image)}.spdx.json"
        cell_steps.append(
            Steps(
                title=f"Attest {image}",
                steps=(
                    SyftTask(
                        image=image,
                        output_path=sbom_path,
                        docker_config=docker_config,
                        executor=executor,
                        role=role,
                    ),
                    CosignTask(
                        operation="sign",
                        image=image,
                        key_file=cosign_key,
                        password_file=password_file,
                        docker_config=docker_config,
                        executor=executor,
                        role=role,
                    ),
                    CosignTask(
                        operation="attest",
                        image=image,
                        key_file=cosign_key,
                        password_file=password_file,
                        docker_config=docker_config,
                        predicate_file=predicate_remote,
                        executor=executor,
                        role=role,
                    ),
                    CosignTask(
                        operation="attach sbom",
                        image=image,
                        key_file=cosign_key,
                        password_file=password_file,
                        docker_config=docker_config,
                        sbom_file=sbom_path,
                        executor=executor,
                        role=role,
                    ),
                    CosignTask(
                        operation="verify-attestation",
                        image=image,
                        key_file=cosign_key,
                        password_file=password_file,
                        docker_config=docker_config,
                        public_key_file=public_key_remote,
                        executor=executor,
                        role=role,
                    ),
                ),
            )
        )
    return Steps(title=title, steps=tuple(cell_steps))


def _artifact_slug(reference: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", reference.split("/")[-1])
```

Aggiungere `import re` in cima al modulo. Lo slug è la stessa regex di `release/attest.py:218`, ma applicata a un input diverso: là riceveva il **tag** (`nanofaas/server:v0.18.1-amd64` → `server-v0.18.1-amd64`), qui riceve il **riferimento pinnato** (`nanofaas/server@sha256:aa…` → `server-sha256-aa…`). I nomi dei file SBOM cambiano rispetto alle release precedenti; è voluto — il digest è ciò che è stato davvero firmato — ma va detto nel commit, perché una release ripresa a cavallo del cambiamento rigenera gli SBOM sotto nomi nuovi invece di riusare i vecchi.

Nota sulle cinque operazioni: `verify` e `verify-attestation` in produzione sono due chiamate separate, ma `verify-attestation` fallisce anche quando la firma manca, quindi il singolo step copre entrambe. Se si preferisce la simmetria esatta con il procedurale, aggiungere un sesto step `operation="verify"` — costa un `docker run` per immagine.

- [ ] **Step 4: Eseguire i test e verificare che passino**

```bash
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -v
```

Atteso: verde.

- [ ] **Step 5: Commit**

```bash
git add packages/sonata-tasks
git commit -m "feat: attest one image at a time, so a resume picks up where it died"
```

---

## Task 9: Cablare il composite di attestazione ed emettere evidenza di firma

`receipts/attest.json` del canary contiene **una sola** voce: il `file-digest` del predicate (`plans/release.py:754`). Nessuna evidenza di SBOM, firma o verifica. `require_attestation_predicate` (`release/tasks.py:250`) verifica quindi che il predicate sia coerente, ma **nulla nella catena di receipt prova che la firma sia mai avvenuta**: una release in cui cosign fallisce in modo non fatale produce un receipt indistinguibile da una firmata.

Nello stesso punto, la closure `attest_images` fa due shell-out inline (`plans/release.py:735-744`) invece di usare `FileTransferTask` (`sonata-tasks/src/sonata_tasks/transfer.py:11`), che esiste già ed è journalizzato.

**Files:**
- Modify: `packages/nanolab/src/nanolab/plans/release.py:712-763`
- Modify: `packages/nanolab/src/nanolab/release/attest.py` — cancellare `attest_release_images:69`, `_cosign:248`, `_write_public_key:222`
- Test: `packages/nanolab/tests/release/test_attest.py`, `packages/nanolab/tests/plans/test_release.py`

**Interfaces:**
- Consumes: `attest_composite` (Task 8), `FileTransferTask` da `sonata_tasks.transfer`
- Produces: la fase `attest` emette `Evidence("cosign-attestation", <pinned reference>, <digest>)` per ogni digest firmato, oltre al `file-digest` del predicate.

- [ ] **Step 1: Scrivere il test che fallisce**

In `packages/nanolab/tests/plans/test_release.py`:

```python
def test_attest_phase_records_one_signature_per_pinned_digest(release_request) -> None:
    workflow = build_release_workflow(release_request, provider=_FakeProvider())
    phase = _phase_named(workflow, "attest")

    evidence = _run_phase(phase)

    signatures = [item for item in evidence if item.kind == "cosign-attestation"]
    assert signatures, "attest produced no signing evidence"
    assert all(item.reference.count("@sha256:") == 1 for item in signatures), (
        "signing evidence must name digest-pinned references"
    )
    assert any(item.kind == "file-digest" for item in evidence), "predicate evidence lost"
```

- [ ] **Step 2: Eseguire il test e verificare che fallisca**

```bash
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/plans/test_release.py -v -k signature
```

Atteso: FAIL — nessuna evidenza `cosign-attestation`.

- [ ] **Step 3: Riscrivere la closure `attest_images`**

In `plans/release.py`, sostituire le righe 712-754:

```python
    predicate_file = release_dir / "predicate.json"
    remote_predicate = f"{remote_root}/predicate.json"
    remote_sboms = f"{remote_root}/sboms"
    remote_public_key = f"{remote_sboms}/cosign.pub"

    def _pinned(images: Mapping[str, str]) -> tuple[str, ...]:
        """Collapse tags and aliases onto the unique set of pinned digests.

        Aliases point at the same digest as their native manifest, so signing
        by reference would sign the same artifact several times.
        """
        pinned: dict[str, None] = {}
        for reference, digest in sorted(images.items()):
            pinned.setdefault(f"{reference.rsplit(':', 1)[0]}@{digest}", None)
        return tuple(pinned)

    def attest_images(inputs: Any) -> tuple[Evidence, ...]:
        if cosign is None:
            raise ValueError("release Cosign credentials are required for attestation")
        release_record()
        images = all_published()
        aggregate_evidence = verified_file_receipt(
            aggregate.receipt, "aggregate", release_dir / "aggregate.json"
        )
        predicate_file.write_text(
            release_attest.render_predicate(
                release_attest.build_release_predicate(
                    version=request.version,
                    source_commit=identity.source_commit,
                    azure_profile=request.settings.profile,
                    benchmark_record_digest=aggregate_evidence.digest,
                    image_digests=images,
                )
            ),
            encoding="utf-8",
        )
        credentials = inputs.resource(cosign).value
        if credentials.password_file is None:
            raise ValueError("cosign attestation requires a staged password file")

        # One-shot setup: the SBOM directory, the predicate, and the public half
        # of the signing key -- `cosign verify` rejects the encrypted private
        # key. Not per-image, so not part of the per-image composite.
        # ponytail: re-runs on resume; three cheap calls against ~150 signed ones.
        _run_steps(
            Steps(
                title="Stage attestation inputs",
                steps=(
                    CommandTask(
                        title="Create remote SBOM directory",
                        argv=("mkdir", "-p", remote_sboms),
                        executor=executor,
                        role="stack",
                    ),
                    FileTransferTask(
                        provider=provider,
                        request=stack_req,
                        source=predicate_file,
                        destination=remote_predicate,
                        title="Transfer release predicate",
                    ),
                    CosignTask(
                        operation="public-key",
                        image="",
                        key_file=credentials.key_file,
                        password_file=str(credentials.password_file),
                        docker_config=docker_credentials(inputs).docker_config,
                        output_file=remote_public_key,
                        executor=executor,
                        role="stack",
                    ),
                ),
            ),
            inputs,
        )

        pinned = _pinned(images)
        _run_steps(
            attest_composite(
                pinned,
                predicate_remote=remote_predicate,
                sbom_dir_remote=remote_sboms,
                public_key_remote=remote_public_key,
                cosign_key=credentials.key_file,
                password_file=str(credentials.password_file),
                docker_config=docker_credentials(inputs).docker_config,
                executor=executor,
                role="stack",
            ),
            inputs,
        )

        return (
            Evidence("file-digest", str(predicate_file), digest_path(predicate_file)),
            *(
                Evidence("cosign-attestation", reference, reference.split("@", 1)[1])
                for reference in pinned
            ),
        )
```

Aggiungere gli import: `Steps` da `sonata_engine`, `CommandTask` da `sonata_tasks.command`, `CosignTask` da `sonata_tasks.cosign`, `FileTransferTask` da `sonata_tasks.transfer`, `attest_composite` da `sonata_tasks.release_composites`, `_run_steps` da `nanolab.release.tasks` (esportarlo senza underscore come `run_steps` se il linter obietta), `Mapping` da `collections.abc`.

Verificare la firma reale di `FileTransferTask` (`sonata-tasks/src/sonata_tasks/transfer.py:11`) e adattare i nomi dei parametri: se non accetta `provider`/`request`, usare la forma che accetta.

- [ ] **Step 4: Cancellare l'implementazione procedurale sostituita**

In `release/attest.py`, rimuovere `attest_release_images` (69-176), `_write_public_key` (222-245), `_cosign` (248-293), e `_artifact_slug` (218-219) se nessun altro lo usa nel modulo. Restano `build_release_predicate`, `render_predicate`, `performance_root`, `finalize_release`, `_write_atomic`, `_exec`. Rimuovere gli import ora orfani (`SYFT_IMAGE`, `COSIGN_IMAGE`, `RemoteCosignCredentials` se non più referenziata, `re`).

Cancellare i test corrispondenti in `packages/nanolab/tests/release/test_attest.py`; **non** cancellare quelli su `build_release_predicate` e `finalize_release`.

- [ ] **Step 5: Eseguire tutte le suite**

```bash
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests
uv run --locked --all-packages --all-groups basedpyright --project packages/nanolab
uv run --locked --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
```

Atteso: verde. `test_run_amd64.py` potrebbe rompersi se il runner legacy chiamava `attest_release_images` — in tal caso **saltare al Task 11**, cancellare il runner, e tornare qui: non vale la pena riparare codice che muore due task dopo. Annotare la scelta nel commit.

- [ ] **Step 6: Commit**

```bash
git add packages/nanolab
git commit -m "feat: prove the release was signed, one receipt entry per digest"
```

---

## Task 10: Cancellare i cinque composite morti

`release_composites.py` esporta 9 composite (`__all__:41-49`); dopo i Task 2, 4 e 9 il percorso di release ne usa 3 (`command_specs_composite`, `registry_push_composite`, `attest_composite`). I cinque rimanenti non hanno chiamanti non-test, e uno (`arm64_smoke_composite`) non ne ha affatto.

Non sono "da cablare più avanti": sono **peggiori** delle funzioni procedurali che il DAG usa davvero.

| Composite | Perché cancellarlo |
|---|---|
| `arm64_build_composite:255` | Il suo commento `ponytail:` (`:291`) ammette che esegue tutto come un unico batch — zero guadagno di resume. L'helper procedurale ha in più verifica architettura, push e digest pinning. |
| `arm64_smoke_composite:356` | Più debole del procedurale: porte host fisse `-p 8080:8080`, watchdog senza `--platform`, nessun pinning per digest, nessun match sull'output. |
| `publish_architectures_composite:454` | Il procedurale porta `require_publication_evidence` (`publish.py:134`) e sorgenti pinnate per digest. |
| `publish_manifests_composite:496` | Il procedurale porta `require_dual_architecture` (`publish.py:293`), che rifiuta un manifest list monoarchitettura. |
| `publish_aliases_composite:555` | Nove comandi: il resume per-step non paga niente. |

È esattamente ciò che chiede il Task 7 del piano di migrazione (`docs/plans/2026-08-01-sonata-release-migration.md:266-292`): *"Prefer the tested domain helpers; delete unused composite code rather than retaining two implementations."*

**Files:**
- Modify: `packages/sonata-tasks/src/sonata_tasks/release_composites.py` — cancellare le sezioni 4-8
- Modify: `packages/sonata-tasks/src/sonata_tasks/__init__.py`
- Modify: `packages/sonata-tasks/tests/test_release_composites.py`

**Interfaces:** nessuna nuova. `__all__` si riduce a `("command_specs_composite", "registry_push_composite", "attest_composite")`.

- [ ] **Step 1: Verificare che davvero nessuno li usi**

```bash
grep -rn "arm64_build_composite\|arm64_smoke_composite\|publish_architectures_composite\|publish_manifests_composite\|publish_aliases_composite" \
  packages/ --include="*.py" | grep -v "packages/sonata-tasks/tests/" | grep -v "release_composites.py"
```

Atteso: **nessun output**. Se qualcosa appare, fermarsi e capire chi lo usa prima di cancellare.

- [ ] **Step 2: Cancellare le cinque funzioni**

In `release_composites.py`, rimuovere le sezioni numerate 4, 5, 6, 7, 8 (righe ~250-596). Aggiornare `__all__` e la numerazione dei commenti-sezione rimasti. Rimuovere gli import che restano orfani — verificare con:

```bash
uv run --locked --all-packages --all-groups ruff check packages/sonata-tasks/src/sonata_tasks/release_composites.py
```

- [ ] **Step 3: Cancellare i loro test**

In `test_release_composites.py`, rimuovere ogni test che nomina uno dei cinque, e i loro import. Rimuovere i doppi (`_FakeCell`, `_FakePlan`, …) che restano senza utilizzatori.

- [ ] **Step 4: Eseguire le suite e i controlli statici**

```bash
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests
uv run --locked --all-packages --all-groups ruff check packages/
uv run --locked --all-packages --all-groups basedpyright --project packages/sonata-tasks
```

Atteso: verde. Il file scende da 677 righe a circa 250.

- [ ] **Step 5: Commit**

```bash
git add packages/sonata-tasks
git commit -m "refactor: delete the composites the release never chose"
```

---

## Task 11: Un solo percorso di esecuzione, un solo journal

Il canary Azure 0.18.1 è girato sul percorso Sonata (`packages/nanolab/runs/canary/releases/0.18.1/sonata.jsonl` più 15 receipt). La condizione posta dal Task 10 del piano di migrazione è soddisfatta, e il suo criterio di accettazione (`docs/plans/2026-08-01-sonata-release-migration.md:23` — *"The final code has one run path and one journal implementation"*) è ancora non rispettato.

**Attenzione all'ordine:** `plans/release.py:58-66` importa `Amd64ReleasePlan`, `BuilderConfiguration`, `CredentialFiles`, `ReleaseSettings`, `git_state` da `release/run.py` e `ReleaseIdentity`, `digest_path` da `release/state.py`. Le dataclass vanno **estratte prima**, o la cancellazione rompe il percorso vivo.

**Files:**
- Create: `packages/nanolab/src/nanolab/release/model.py`
- Delete: `packages/nanolab/src/nanolab/release/run.py`, `packages/nanolab/src/nanolab/release/state.py`
- Delete: `packages/nanolab/tests/release/test_run_amd64.py`, `packages/nanolab/tests/release/test_state.py`
- Modify: `packages/nanolab/src/nanolab/cli/release.py` — solo `prepare` sopravvive
- Modify: `packages/nanolab/src/nanolab/release/build.py`, `benchmark.py`, `attest.py`, `publish.py`, `resources.py`, `tasks.py`, `plans/release.py`, `cli/product.py` — aggiornare gli import

**Interfaces:**
- Produces: `nanolab.release.model` esporta `GitState`, `git_state`, `CredentialFiles`, `BuilderConfiguration`, `ReleaseSettings`, `Amd64ReleasePlan`, `ReleaseIdentity`, `digest_path`. `Amd64ReleasePlan` **perde** `phase_names`, `render` e `state_directory` (erano solo del runner).

- [ ] **Step 1: Creare `release/model.py` con le dataclass condivise**

Spostare per copia letterale, senza modificarne il corpo:

- da `run.py`: `GitState`, `CredentialFiles` (110-138), `BuilderConfiguration` (141-145), `ReleaseSettings` (147-157), `Amd64ReleasePlan` (159-208) **senza** `phase_names`, `render`, `state_directory`, e `git_state` (210-225)
- da `state.py`: `ReleaseIdentity` e `digest_path`

Aggiungere una docstring che dica cosa è questo modulo:

```python
"""The release plan data shared by everything that reads a release.

Extracted from the procedural runner it used to live in: these are plain
descriptions of what a release is, not part of how one executes.
"""
```

- [ ] **Step 2: Reindirizzare tutti gli import**

```bash
grep -rln "from nanolab.release.run import\|from nanolab.release.state import\|from nanolab.release import run\b" \
  packages/nanolab/src packages/nanolab/tests
```

Per ciascun file, cambiare la sorgente in `nanolab.release.model`. Non cambiare i nomi importati.

- [ ] **Step 3: Verificare che il percorso Sonata regga senza toccare run.py**

```bash
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests
```

Atteso: verde, `run.py` e `state.py` sono ancora sul disco ma nessuno importa più le dataclass da lì.

- [ ] **Step 4: Commit intermedio — l'estrazione è di per sé sicura**

```bash
git add packages/nanolab
git commit -m "refactor: separate what a release is from how one is run"
```

- [ ] **Step 5: Ridurre la CLI a `release prepare`**

In `packages/nanolab/src/nanolab/cli/release.py`, cancellare `plan_command` (59-78) e `run_command` (80-118), più `_release_plan` e gli import ora orfani. Il file scende a circa 40 righe. Aggiornare l'help del sotto-comando:

```python
    release = typer.Typer(help="Prepare a version for the guarded Azure image release.")
```

`nanolab release run` non esiste più: la release si lancia con `nanolab run scenarios-v2/release.yaml`.

- [ ] **Step 6: Cancellare il runner e il suo journal**

```bash
git rm packages/nanolab/src/nanolab/release/run.py \
       packages/nanolab/src/nanolab/release/state.py \
       packages/nanolab/tests/release/test_run_amd64.py \
       packages/nanolab/tests/release/test_state.py
```

Muore con loro la stringa obsoleta `run.py:199` — *"ARM64: digest-pinned QEMU after regression-gate"* — falsa da quando il builder ARM è nativo (`environments/azure-release.yaml:31-33`, `Standard_D8ps_v5`, *"~6x faster per native-image cell"*).

- [ ] **Step 7: Ripulire le funzioni procedurali rimaste senza chiamanti**

In `release/build.py` cadono con il runner: `_run_source_tests` (463), `_build_amd64_images` (496), `_push_local_images` (813), `_verify_generated_build_inputs` (776), e i re-export di compatibilità a `run.py:57-73`. **Restano** `_build_arm64_images` (528) e `_smoke_arm64_images` (606): il DAG li chiama.

Verificare caso per caso prima di cancellare:

```bash
grep -rn "_run_source_tests\|_build_amd64_images\|_push_local_images\|_verify_generated_build_inputs" \
  packages/nanolab/src packages/nanolab/tests
```

Cancellare solo ciò che non ha più chiamanti fuori dai test cancellati.

- [ ] **Step 8: Rompere le indirezioni che esistevano solo per il ciclo di import**

`release/run.py:57-73` e i `_coordinator()` in `build.py`/`benchmark.py` con `# noqa: F401 - compatibility re-exports` esistevano solo per spezzare il ciclo `run ↔ benchmark ↔ build`. Senza `run.py` il ciclo non c'è più: rimuoverli e togliere le `noqa`.

```bash
grep -rn "compatibility re-exports\|_coordinator" packages/nanolab/src
uv run --locked --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
```

Atteso: import-linter verde con meno eccezioni di prima.

- [ ] **Step 9: Aggiornare la docstring del modulo di piano**

`plans/release.py:3` dice ancora *"The release pipeline defined in `nanolab release run` replays here"*. Quel comando non esiste più:

```python
"""Compile a release scenario into a Sonata workflow.

The release pipeline is a linear DAG built from the Task and Resource
primitives defined in sonata-tasks. Each phase that iterates over image cells
is a composite Steps node so the workflow surface stays coarse-grained and
selectable.
"""
```

- [ ] **Step 10: Suite completa e controlli statici**

```bash
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run --locked --all-packages --all-groups \
  pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests
uv run --locked --all-packages --all-groups ruff check packages/
uv run --locked --all-packages --all-groups basedpyright --project packages/nanolab
uv run --locked --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
```

Atteso: verde ovunque.

- [ ] **Step 11: Verificare la superficie CLI**

```bash
cd packages/nanolab && NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run nanolab release --help
```

Atteso: solo `prepare`.

```bash
cd packages/nanolab && NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run nanolab plan \
  scenarios-v2/release.yaml --environment environments/azure-release.yaml
```

Atteso: 15 fasi, nessun errore.

- [ ] **Step 12: Commit**

```bash
git add -A packages/nanolab
git commit -m "refactor: one release path, one journal"
```

---

## Verifica finale end-to-end

I task sopra si verificano da soli sui test. Queste due prove richiedono Azure e vanno fatte una volta sola, alla fine.

- [ ] **Prova 1: canary su una versione nuova**

```bash
cd packages/nanolab
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run nanolab run scenarios-v2/release.yaml \
  --environment environments/azure-release.yaml --provision --run-dir runs/canary
```

Confrontare i receipt AMD64 e ARM64 prodotti:

```bash
python - <<'PY'
import json, pathlib
d = pathlib.Path("runs/canary/releases/<version>/receipts")
for name in ("amd64-build.json", "arm64-build.json"):
    payload = json.loads((d / name).read_text())
    kinds = {item["kind"] for item in payload["evidence"]}
    print(name, len(payload["evidence"]), kinds)
PY
```

Atteso: stesso numero di voci per le due architetture, e nessuna delle due con `"evidence": []`.

Verificare che `attest.json` non contenga più solo il predicate:

```bash
python -c "import json;p=json.load(open('runs/canary/releases/<version>/receipts/attest.json'));print({e['kind'] for e in p['evidence']}, len(p['evidence']))"
```

Atteso: `{'file-digest', 'cosign-attestation'}` e un conteggio pari a 1 + il numero di digest unici pubblicati.

- [ ] **Prova 2: il resume di `attest` riprende, non ricomincia**

Rilanciare il canary interrompendolo (`Ctrl-C`) a metà della fase `attest`, poi:

```bash
NANOFAAS_ROOT=${NANOFAAS_ROOT:?} uv run nanolab run scenarios-v2/release.yaml \
  --environment environments/azure-release.yaml --resume --run-dir runs/canary
```

Contare gli step di attestazione rieseguiti nel journal:

```bash
grep -c "attest/" runs/canary/releases/<version>/sonata.jsonl
```

Atteso: al secondo giro il journal registra **solo** gli step per le immagini non ancora firmate. Se le rifà tutte, il composite del Task 8 non sta ricevendo lo `_step_scope` — verificare che `_run_steps` passi il `TaskInputs` della fase e non uno costruito a mano.

- [ ] **Prova 3: verificare a mano una firma pubblicata**

```bash
cosign verify --key <public-key> ghcr.io/<org>/nanofaas/<image>@<digest>
cosign verify-attestation --key <public-key> --type custom ghcr.io/<org>/nanofaas/<image>@<digest>
```

Atteso: entrambe passano. Questa è la prova che i flag del Task 7 sono corretti — è l'unico controllo che i test unitari non possono dare.

---

## Note di esecuzione

**Ordine.** I task 1-6 sono la correttezza AMD64 e vanno in ordine (3 dipende da 2, 4 da 2 e 3, 5 da 4). I task 7-9 sono l'attestazione e vanno in ordine. Il 10 dipende da 4 e 9. L'11 dipende da tutti.

**Punto di non ritorno.** Fino al Task 10 compreso, il runner legacy resta come rete di sicurezza: se il canary del Task 4 fallisce su Azure, `nanolab release run` è ancora lì. Il Task 11 la toglie — eseguirlo solo dopo un canary verde.

**Se `test_run_amd64.py` intralcia.** Diversi task toccano codice che il runner legacy condivide. Se ripararlo costa più di venti minuti, saltare al Task 11, cancellarlo, e tornare indietro: sta morendo comunque.

**Fuori scope.** `plans/_assembly.py:11-27` e `cli/provisioning.py:57-348` costruiscono ancora la `Workflow` legacy di `workflow_tasks.core.workflow`. Non è nel percorso di release e non blocca niente qui.
