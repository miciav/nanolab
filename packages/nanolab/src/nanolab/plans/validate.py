from dataclasses import replace
import json
from pathlib import Path

from sonata_engine import Workflow
from sonata_tasks.compose import DockerComposeProject, docker_compose_resource
from sonata_tasks.deployment import LOCAL_REGISTRY
from sonata_tasks.registry import docker_registry_resource
from sonata_tasks.http_function import HttpFunctionExpectation
from sonata_tasks.validate_recovery import managed_container_cleanup_resource
from sonata_tasks.validate import AsyncCheck, EnvelopeCheck, ValidateFunction as SonataFunction
from sonata_tasks.validate import ValidateWorkflowRequest, build_validate_workflow
from sonata_tasks.components.helm import control_plane_helm_values, helm_set_args
from sonata_tasks.execution.bindings import RoleBindings, RoleBoundCommandTaskExecutor

from nanolab.config.scenario import ScenarioConfig
from nanolab.config.environment import EnvironmentConfig
from nanolab.plans.functions import resolve_function, resolve_function_payloads, sonata_function
from nanolab.workspace.paths import discover_tool_root
from nanolab.workspace.provenance import source_fingerprint


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_QR_CODES = ("qr-code-exec", "qr-code-go", "qr-code-java", "qr-code-javascript", "qr-code-python")
_ROMAN_NUMERALS = (
    "roman-numeral-exec",
    "roman-numeral-go",
    "roman-numeral-java",
    "roman-numeral-javascript",
    "roman-numeral-python",
)
_JSON_TRANSFORMS = (
    "json-transform-exec",
    "json-transform-go",
    "json-transform-java",
    "json-transform-javascript",
    "json-transform-python",
)
_HEADER_ENVELOPE_PROBES = (
    "handler-envelope-exec",
    "handler-envelope-go",
    "handler-envelope-java",
    "handler-envelope-javascript",
    "handler-envelope-python",
)
_BINARY_ENVELOPE_PROBE = "binary-envelope-java"
_HANDLER_ENVELOPE_TARGETS = (
    *_QR_CODES, *_ROMAN_NUMERALS, *_JSON_TRANSFORMS, *_HEADER_ENVELOPE_PROBES,
    _BINARY_ENVELOPE_PROBE, "word-stats-java",
)
_EMPTY_INPUT_PAYLOAD = '{"input":{}}'
_ASYNC_CONTROL_PLANE_MODULES = "container-deployment-provider,async-queue"


def _handler_envelope_checks(functions: dict[str, SonataFunction]) -> tuple[EnvelopeCheck, ...]:
    missing = [key for key in _HANDLER_ENVELOPE_TARGETS if key not in functions]
    if missing:
        raise ValueError(f"handler envelope requires {', '.join(missing)}")

    def name(key: str) -> str:
        return functions[key].name

    marker = (("X-NanoFaaS-Function-Status", "true"),)
    return (
        *(
            EnvelopeCheck(
                name(key),
                '{"input":{"text":"https://example.org/invite/abc","size":256}}',
                HttpFunctionExpectation(
                    status=200,
                    api_status="success",
                    status_code=200,
                    required_headers=(
                        *marker,
                        ("Content-Type", "application/json"),
                        ("X-NanoFaaS-Encoding", "base64"),
                    ),
                    forbidden_header_values=(("Content-Type", "image/png"),),
                    api_headers={"Content-Type": "image/png"},
                    encoding="base64",
                    decoded_prefix=_PNG_SIGNATURE,
                ),
            )
            for key in _QR_CODES
        ),
        *(
            EnvelopeCheck(
                name(key),
                _EMPTY_INPUT_PAYLOAD,
                HttpFunctionExpectation(
                    status=422,
                    api_status="success",
                    output={"error": "missing required field: number"},
                    status_code=422,
                    required_headers=marker,
                ),
            )
            for key in _ROMAN_NUMERALS
        ),
        *(
            EnvelopeCheck(
                name(key),
                _EMPTY_INPUT_PAYLOAD,
                HttpFunctionExpectation(
                    status=400,
                    api_status="success",
                    output={"error": "Fields 'data' (array) and 'groupBy' (string) are required"},
                    status_code=400,
                    required_headers=marker,
                ),
            )
            for key in _JSON_TRANSFORMS
        ),
        *(
            EnvelopeCheck(
                name(key),
                '{"input":{"message":"body-sentinel"},"headers":{"x-e2e-token":"forged"}}',
                HttpFunctionExpectation(
                    status=200,
                    api_status="success",
                    output={"body": "body-sentinel", "header": "header-sentinel"},
                    status_code=None,
                    api_headers=None,
                    encoding=None,
                ),
                headers=("X-E2E-Token: header-sentinel",),
            )
            for key in _HEADER_ENVELOPE_PROBES
        ),
        EnvelopeCheck(
            name(_BINARY_ENVELOPE_PROBE),
            _EMPTY_INPUT_PAYLOAD,
            HttpFunctionExpectation(
                status=200, api_status="success", status_code=200,
                required_headers=(
                    *marker,
                    ("Content-Type", "application/json"),
                    ("X-NanoFaaS-Encoding", "base64"),
                ),
                forbidden_header_values=(("Content-Type", "application/octet-stream"),),
                api_headers={"Content-Type": "application/octet-stream"},
                encoding="base64", decoded_bytes=b"\x00\x01\x02",
            ),
        ),
        # Java Lite is deliberately exercised by its ordinary invoke only: its
        # binding was not part of the envelope changes covered by this scenario.
        EnvelopeCheck(
            name("word-stats-java"),
            functions["word-stats-java"].payload,
            HttpFunctionExpectation(
                status=200,
                api_status="success",
                status_code=None,
                api_headers=None,
                encoding=None,
                forbidden_headers=("X-NanoFaaS-Function-Status", "X-NanoFaaS-Encoding"),
            ),
        ),
    )


def _async_checks(
    functions: dict[str, SonataFunction], config: ScenarioConfig, repo_root: Path | None
) -> tuple[AsyncCheck, ...]:
    """One async check per payload file each selected function owns.

    Reads the payload set from the nanoFaaS checkout (`functions/<runtime>/<family>/payloads/`),
    wrapping each raw input in the `{"input": ...}` envelope the control plane expects.
    """
    checks: list[AsyncCheck] = []
    for key in config.functions:
        name = functions[key].name
        for payload in resolve_function_payloads(key, source_root=repo_root):
            checks.append(
                AsyncCheck(
                    function_name=name,
                    payload=json.dumps({"input": payload.input}, separators=(",", ":")),
                    expected_output=payload.expected,
                    payload_name=payload.name,
                )
            )
    return tuple(checks)


def build_validate_plan(  # NOSONAR (S3776): backend-specific resource graph is intentionally co-located
    config: ScenarioConfig,
    bindings: RoleBindings,
    *,
    repo_root: Path | None = None,
    tool_root: Path | None = None,
    environment: EnvironmentConfig | None = None,
) -> Workflow:
    """Compile the validate scenario into a Sonata workflow.

    The legacy builder had to take the rendered specs apart to work: it filtered
    out `container.start.control-plane` by task id, built the rest, then inserted
    a resource back at the index the removed spec had occupied, and finally
    attached a separate cleanup list the runner had to remember to execute. The
    control plane is now a resource the workflow is given, and the teardown is
    the release half of the resources it holds.
    """
    if config.workflow != "validate" or config.backend is None:
        raise ValueError("validate plan requires a validate scenario with a backend")
    root = repo_root or Path.cwd()
    kubernetes = config.backend == "k8s"
    functions = {
        key: sonata_function(
            resolve_function(config, key, source_root=repo_root, tool_root=tool_root)
        )
        for key in config.functions
    }
    request = ValidateWorkflowRequest(
        backend=config.backend,
        build=config.build,
        functions=tuple(functions.values()),
        envelope_checks=_handler_envelope_checks(functions) if config.handler_envelope else (),
        async_checks=_async_checks(functions, config, repo_root) if config.async_load else (),
        additional_modules=("sync-queue",) if kubernetes else (),
        source_fingerprint=source_fingerprint(root),
        build_control_plane=kubernetes,
        push_function_images=not kubernetes,
        persistent_recovery=config.persistent_recovery,
    )
    if kubernetes:
        product_root = tool_root or discover_tool_root()
        if environment is not None and environment.provider != "local":
            target = environment.target("stack")
            queue_burst_script = Path(target.remote_home) / "nanolab-assets/k6/k8s-queue-burst.js"
        else:
            queue_burst_script = product_root / "assets/k6/k8s-queue-burst.js"
        request = replace(
            request,
            queue_probe=SonataFunction(
                name="k8s-sync-queue",
                image=f"{LOCAL_REGISTRY}/nanofaas/java-warm-echo:e2e",
                payload='{"input":{"message":"warmup"}}',
                build_argv=("./gradlew", ":services:java:warm-echo:bootJar", "--quiet"),
                image_build_argv=("docker", "build", "-t", f"{LOCAL_REGISTRY}/nanofaas/java-warm-echo:e2e", "-f", "services/java/warm-echo/Dockerfile", "services/java/warm-echo"),
                concurrency=1,
            ),
            queue_burst_script=queue_burst_script,
        )
        # Both settings are what this workflow exists to exercise: the JUnit queue
        # contracts need admission on, and the metric assertions need the advanced
        # profile. Derived from the request so the chart and the pushed image can
        # never name different things.
        request = replace(
            request,
            helm_values=helm_set_args(
                control_plane_helm_values(
                    namespace=request.namespace,
                    control_plane_image=request.control_plane_image_reference(),
                    metrics_profile="advanced",
                    sync_queue_admission_enabled=True,
                )
            ),
        )
    requires = ()
    if not kubernetes:
        registry = docker_registry_resource(
            executor=RoleBoundCommandTaskExecutor(bindings),
            role="host",
        )
        project = DockerComposeProject(
            name="nanofaas-recovery" if config.persistent_recovery else "nanofaas-validate",
            file=Path("deploy/compose/compose.yaml"),
            ready_url="http://127.0.0.1:8081/actuator/health/readiness",
            env=(
                {"NANOFAAS_CONTROL_PLANE_MODULES": _ASYNC_CONTROL_PLANE_MODULES}
                if config.async_load
                else {}
            ),
        )
        cleanup = (
            managed_container_cleanup_resource(
                tuple(function.name for function in functions.values()),
                executor=RoleBoundCommandTaskExecutor(bindings),
                role="host",
                cwd=root,
            )
            if config.persistent_recovery
            else None
        )
        compose = docker_compose_resource(
            project,
            executor=RoleBoundCommandTaskExecutor(bindings),
            cwd=root,
            requires=(registry,) if cleanup is None else (registry, cleanup),
        )
        requires = (registry, compose) if cleanup is None else (registry, cleanup, compose)
        if config.persistent_recovery:
            request = replace(request, recovery_project=project)
    return build_validate_workflow(
        request,
        bindings,
        cwd=root,
        local_endpoint="http://127.0.0.1:8080",
        requires=requires,
    )
