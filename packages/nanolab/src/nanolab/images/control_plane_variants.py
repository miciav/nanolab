"""The control-plane builds a comparison run puts side by side.

A variant is only a way of *building* the same source: same modules, same
configuration, same functions. Everything that differs is passed to the build,
so a difference in the results has one candidate explanation rather than five.

The build happens on the VM under test, not here. Native images are compiled for
the machine that runs them and cannot be cross-built from an arm64 laptop for an
amd64 node; more quietly, `-O3` inlines against the target's instruction set, so
even a same-architecture build made elsewhere is not the artefact being measured.

Nothing in this module builds a *function* image. The functions are held fixed
across variants on purpose: the question is what the control plane costs, and a
function rebuilt per variant would put a second moving part in every comparison.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from sonata_tasks.components.operations import RemoteCommandOperation


@dataclass(frozen=True, slots=True)
class ControlPlaneVariant:
    """One way of building the control plane, and what to call the result."""

    key: str
    label: str
    # Why this build is in the matrix at all. Carried into the report so a
    # reader does not have to reconstruct the intent from the flags.
    rationale: str
    build_env: Mapping[str, str]

    def image(self, registry: str) -> str:
        return f"{registry}/nanofaas/control-plane:{self.key}"


def _env(**values: str) -> Mapping[str, str]:
    return MappingProxyType(dict(values))


# Serial is not written out for the Community builds: it is the only collector
# GraalVM CE offers besides epsilon, and naming it would suggest a choice was
# made. G1 is Oracle-only, which is why that variant also switches distribution.
VARIANTS: tuple[ControlPlaneVariant, ...] = (
    ControlPlaneVariant(
        key="jvm",
        label="JVM (Java 25, JIT)",
        rationale=(
            "The baseline everything else is compared against, and the only build "
            "that JIT-compiles from a profile gathered during the run itself."
        ),
        build_env=_env(),
    ),
    # The baseline's own tuning is not the JVM's default. The image pins
    # -XX:+UseSerialGC -XX:TieredStopAtLevel=1, chosen when the chart gave the
    # control plane a single core, and the running process confirms it: the
    # actuator reports the serial collector's MXBeans even at two cores. Dropping
    # TieredStopAtLevel restores the default of 4, so the flag is written out
    # where C1-only is meant and omitted where it is not.
    #
    # Without this variant the comparison is optimised AOT against a JIT with its
    # optimising compiler switched off, which is not the question the chapter
    # asks. The 2026-08 archive prices the difference at 40% of throughput and a
    # factor of six on p95 at one core.
    ControlPlaneVariant(
        key="jvm-c2",
        label="JVM (serial GC, full tiering)",
        rationale="Isolates the JIT: C2 restored, collector held at the baseline's serial.",
        build_env=_env(JVM_TUNING="-XX:+UseSerialGC"),
    ),
    ControlPlaneVariant(
        key="native-os",
        label="Native, -Os, serial GC",
        rationale=(
            "Optimised for image size. Halves the binary against -O3 and was the "
            "default until measurement showed the size is paid on the registry "
            "rather than on the node."
        ),
        build_env=_env(NATIVE_OPTIMIZATION="s"),
    ),
    ControlPlaneVariant(
        key="native-o3",
        label="Native, -O3, serial GC",
        rationale=(
            "The current default. Faster than -Os where memory is not the "
            "constraint, and identical to it where it is — the serial collector "
            "becomes the bottleneck before the code quality does."
        ),
        build_env=_env(NATIVE_OPTIMIZATION="3"),
    ),
    ControlPlaneVariant(
        key="native-o3-g1",
        label="Native, -O3, G1 (Oracle GraalVM)",
        rationale=(
            "The variant that tests whether the collector was the limit: under "
            "sustained load the serial collector spent 32% of wall-clock time "
            "collecting, with pauses reaching 1.9s."
        ),
        build_env=_env(NATIVE_OPTIMIZATION="3", NATIVE_GC="G1"),
    ),
)

VARIANTS_BY_KEY: Mapping[str, ControlPlaneVariant] = MappingProxyType(
    {variant.key: variant for variant in VARIANTS}
)


def resolve_variants(keys: tuple[str, ...]) -> tuple[ControlPlaneVariant, ...]:
    unknown = [key for key in keys if key not in VARIANTS_BY_KEY]
    if unknown:
        raise ValueError(
            "unknown control-plane variants: "
            + ", ".join(unknown)
            + ". Available: "
            + ", ".join(VARIANTS_BY_KEY)
        )
    return tuple(VARIANTS_BY_KEY[key] for key in keys)


def build_operations(
    variant: ControlPlaneVariant,
    *,
    registry: str,
    modules: str,
    build_memory: str | None = None,
    parallelism: int | None = None,
) -> tuple[RemoteCommandOperation, ...]:
    """Commands that produce this variant's image on the VM and publish it.

    The image is pushed to the VM-local registry because k3s pulls from there;
    a locally tagged image is invisible to containerd.
    """
    image = variant.image(registry)
    if variant.key.startswith("jvm"):
        tuning = variant.build_env.get("JVM_TUNING")
        # Only passed when the variant asks for it, so the baseline's build line
        # stays exactly what it was and its cache key does not move.
        tuning_args = ("--build-arg", f"JVM_TUNING={tuning}") if tuning else ()
        return (
            RemoteCommandOperation(
                operation_id=f"variant.{variant.key}.boot_jar",
                summary=f"Build boot jar for {variant.label}",
                argv=(
                    "./gradlew",
                    ":control-plane:bootJar",
                    f"-PcontrolPlaneModules={modules}",
                    "--no-daemon",
                    "-q",
                ),
                env=_env(),
                execution_target="vm",
            ),
            RemoteCommandOperation(
                operation_id=f"variant.{variant.key}.image",
                summary=f"Build image for {variant.label}",
                argv=(
                    "docker",
                    "build",
                    "-f",
                    "platform/control-plane/Dockerfile",
                    *tuning_args,
                    "-t",
                    image,
                    "platform/control-plane",
                ),
                env=_env(),
                execution_target="vm",
            ),
            _push(variant, image),
        )
    # native-java-image.sh reads the optimisation level, the collector and the
    # distribution from the environment, and switches to Oracle GraalVM by itself
    # when G1 is asked for: Community rejects --gc=G1 at build time rather than
    # falling back, which is the behaviour worth keeping.
    #
    # The memory bound is for the BUILDER. native-image sizes its own heap from
    # the machine's total memory and cannot see what else is running: on a 12GB
    # VM already holding k3s, a control plane and Prometheus it was OOM-killed
    # after 9m50s, reporting only "exit 137".
    limits: dict[str, str] = {}
    if build_memory:
        limits["NATIVE_BUILD_MEMORY"] = build_memory
    if parallelism:
        limits["NATIVE_PARALLELISM"] = str(parallelism)
    return (
        RemoteCommandOperation(
            operation_id=f"variant.{variant.key}.image",
            summary=f"Compile native image for {variant.label}",
            argv=("./scripts/native-java-image.sh", "control-plane", image),
            env=_env(CONTROL_PLANE_MODULES=modules, **dict(variant.build_env), **limits),
            execution_target="vm",
        ),
        _push(variant, image),
    )


def _push(variant: ControlPlaneVariant, image: str) -> RemoteCommandOperation:
    return RemoteCommandOperation(
        operation_id=f"variant.{variant.key}.push",
        summary=f"Push image for {variant.label}",
        argv=("docker", "push", image),
        env=_env(),
        execution_target="vm",
    )
