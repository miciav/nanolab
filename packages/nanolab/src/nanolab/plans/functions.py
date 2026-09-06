"""How a scenario's function key becomes something a task can run.

Four plan builders need this, which is why it is a module of its own: it used
to live in `validate` under private names that everyone imported anyway, so the
underscore documented an intent the code contradicted.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import yaml
from nanolab.tasks.validate import ValidateFunction as SonataFunction

from nanolab.config.scenario import ScenarioConfig
from nanolab.functions.catalog import FunctionDefinition, resolve_function_definition
from nanolab.workspace.paths import discover_tool_root


@dataclass(frozen=True, slots=True)
class ResolvedFunction:
    key: str
    name: str
    image: str
    build_argv: tuple[str, ...]
    payload: str
    image_build_argv: tuple[str, ...] | None = None
    resources: dict[str, object] | None = None
    scaling_config: dict[str, object] | None = None
    timeout_ms: int = 5000
    concurrency: int = 2
    queue_size: int = 20
    max_retries: int = 3


@dataclass(frozen=True, slots=True)
class FunctionPayload:
    """One payload file a function owns: raw input and the expected output."""

    name: str
    input: object
    expected: object


def resolve_function_payloads(
    key: str, source_root: Path | None = None
) -> tuple[FunctionPayload, ...]:
    """The payload set a function owns under `functions/<runtime>/<family>/payloads/`.

    Each file is the nanoFaaS function-test shape `{description, input, expected}`.
    Functions without a payload directory (or none at all) contribute nothing.
    """
    definition = resolve_function_definition(key, source_root)
    if definition.example_dir is None:
        return ()
    payload_dir = definition.example_dir / "payloads"
    if not payload_dir.is_dir():
        return ()
    payloads: list[FunctionPayload] = []
    for path in sorted(payload_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        payloads.append(
            FunctionPayload(
                name=path.stem,
                input=data.get("input"),
                expected=data.get("expected"),
            )
        )
    return tuple(payloads)


def _function_image(definition: FunctionDefinition) -> str:
    if definition.default_image is None:
        raise ValueError(f"function {definition.key!r} has no image")
    return definition.default_image


def _build_argv(definition: FunctionDefinition, image: str) -> tuple[str, ...]:
    family = definition.family
    runtime = definition.runtime
    if runtime == "java":
        argv = (
            "./gradlew",
            f":functions:java:{family}:bootJar",
            "--quiet",
        )
    else:
        runtime_dir = {"java-lite": "java", "exec": "bash"}.get(runtime, runtime)
        suffix = "-lite" if runtime == "java-lite" else ""
        argv = (
            "docker",
            "build",
            "-t",
            image,
            "-f",
            f"functions/{runtime_dir}/{family}{suffix}/Dockerfile",
            ".",
        )
    return argv


def _image_build_argv(definition: FunctionDefinition, image: str) -> tuple[str, ...] | None:
    if definition.runtime != "java":
        return None
    family = definition.family
    return (
        "docker", "build", "-t", image, "-f", f"functions/java/{family}/Dockerfile", f"functions/java/{family}"
    )


def _function_name(definition: FunctionDefinition) -> str:
    if definition.example_dir is None:
        return definition.key
    manifest = yaml.safe_load(
        (definition.example_dir / "function.yaml").read_text(encoding="utf-8")
    )
    return str(manifest.get("name", definition.key))


def _payload(definition: FunctionDefinition, tool_root: Path | None = None) -> str:
    if definition.default_payload_file is None:
        return '{"input":{}}'
    product_root = tool_root or discover_tool_root()
    payload_path = (
        product_root / "scenarios" / "payloads" / definition.default_payload_file
    )
    if not payload_path.exists():
        return '{"input":{}}'
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    return json.dumps({"input": payload}, separators=(",", ":"))


def resolve_function(
    config: ScenarioConfig,
    key: str,
    *,
    source_root: Path | None = None,
    tool_root: Path | None = None,
) -> ResolvedFunction:
    definition = resolve_function_definition(key, source_root)
    image = _function_image(definition)
    resource = config.resources.get(key)
    return ResolvedFunction(
        key=key,
        name=_function_name(definition),
        image=image,
        build_argv=_build_argv(definition, image),
        image_build_argv=_image_build_argv(definition, image),
        payload=_payload(definition, tool_root),
        resources=(
            resource.model_dump(by_alias=True, exclude_none=True) if resource is not None else None
        ),
    )


def sonata_function(resolved: ResolvedFunction) -> SonataFunction:
    """Convert the product's resolved function to Sonata's public task shape."""
    return SonataFunction(
        name=resolved.name,
        image=resolved.image,
        payload=resolved.payload,
        build_argv=resolved.build_argv,
        image_build_argv=resolved.image_build_argv,
        resources=resolved.resources,
        scaling_config=resolved.scaling_config,
        timeout_ms=resolved.timeout_ms,
        concurrency=resolved.concurrency,
        queue_size=resolved.queue_size,
        max_retries=resolved.max_retries,
    )
