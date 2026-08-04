from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from nanolab.functions.catalog import FunctionDefinition, list_functions
from nanolab.release.versioning import normalize_version


ImageArchitecture = Literal["amd64", "arm64"]
ImageFlavor = Literal["jvm", "native", "default"]

DEFAULT_ARCHITECTURES: tuple[ImageArchitecture, ...] = ("amd64", "arm64")
DEFAULT_REGISTRY = "localhost:5000/nanofaas"

NATIVE_JAVA_DOCKERFILE = Path("deploy/native-java/Dockerfile")


@dataclass(frozen=True)
class NativeBuild:
    """How nanoFaaS builds one Java native image since commit `c3179fbb`.

    These are the three build args `scripts/native-java-image.sh` feeds to the
    shared Dockerfile. nanolab bakes them rather than shelling out to the
    script, so native cells stay inside the single buildx graph the release
    already digest-pins and verifies.
    """

    task: str
    binary: Path
    gradle_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImageTarget:
    name: str
    flavors: tuple[ImageFlavor, ...]
    dockerfile: Path
    context: Path
    native_build: NativeBuild | None = None
    jvm_prerequisite_arguments: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImageCell:
    target: ImageTarget
    architecture: ImageArchitecture
    flavor: ImageFlavor
    tag: str
    image: str

    @property
    def platform(self) -> str:
        return f"linux/{self.architecture}"

    @property
    def native_build(self) -> NativeBuild | None:
        """The native contract for this cell, or None if it is not a Java native cell."""
        if self.flavor != "native":
            return None
        return self.target.native_build

    @property
    def dockerfile(self) -> Path:
        native = self.native_build
        return NATIVE_JAVA_DOCKERFILE if native is not None else self.target.dockerfile

    @property
    def context(self) -> Path:
        # The shared native Dockerfile does `COPY . .` — it only builds from the
        # repository root, never from the target's own directory.
        native = self.native_build
        return Path(".") if native is not None else self.target.context

    @property
    def build_args(self) -> dict[str, str]:
        native = self.native_build
        if native is None:
            return {}
        return {
            "NATIVE_TASK": native.task,
            "NATIVE_BINARY": native.binary.as_posix(),
            "GRADLE_ARGS": " ".join(native.gradle_args),
        }

    @property
    def prerequisite_command(self) -> tuple[str, ...] | None:
        if self.flavor != "jvm":
            return None
        return ("./gradlew", *self.target.jvm_prerequisite_arguments)


@dataclass(frozen=True)
class ImagePlan:
    version: str
    registry: str
    targets: tuple[ImageTarget, ...]
    cells: tuple[ImageCell, ...]

    @property
    def target_names(self) -> frozenset[str]:
        return frozenset(target.name for target in self.targets)


def build_image_plan(
    repo_root: Path,
    version: str,
    *,
    registry: str = DEFAULT_REGISTRY,
    selectors: Sequence[str] = (),
    architectures: Sequence[ImageArchitecture] = DEFAULT_ARCHITECTURES,
    flavors: Sequence[ImageFlavor] = ("jvm", "native", "default"),
) -> ImagePlan:
    """Expand the live repository catalog into immutable image build cells."""
    repo_root = Path(repo_root).resolve()
    _, version_tag = normalize_version(version)
    registry = registry.rstrip("/")
    if not registry:
        raise ValueError("image registry must not be empty")

    targets = _select_targets(_all_targets(repo_root), selectors)
    selected_flavors = frozenset(flavors)
    cells = tuple(
        _cell(target, architecture, flavor, version_tag, registry)
        for architecture in architectures
        for target in targets
        for flavor in target.flavors
        if flavor in selected_flavors
    )
    return ImagePlan(version=version_tag, registry=registry, targets=targets, cells=cells)


def _all_targets(repo_root: Path) -> tuple[ImageTarget, ...]:
    targets = [
        ImageTarget(
            name="control-plane",
            flavors=("jvm", "native"),
            dockerfile=Path("platform/control-plane/Dockerfile"),
            context=Path("platform/control-plane"),
            native_build=NativeBuild(
                task=":control-plane:nativeCompile",
                binary=Path("platform/control-plane/build/native/nativeCompile/control-plane"),
                gradle_args=("-PcontrolPlaneModules=all",),
            ),
            jvm_prerequisite_arguments=(
                ":control-plane:bootJar",
                "-PcontrolPlaneModules=all",
            ),
        ),
        ImageTarget(
            name="java-warm-echo",
            flavors=("jvm", "native"),
            dockerfile=Path("services/java/warm-echo/Dockerfile"),
            context=Path("services/java/warm-echo"),
            native_build=NativeBuild(
                task=":services:java:warm-echo:nativeCompile",
                binary=Path("services/java/warm-echo/build/native/nativeCompile/warm-echo"),
            ),
            jvm_prerequisite_arguments=(":services:java:warm-echo:bootJar",),
        ),
        ImageTarget(
            name="watchdog",
            flavors=("default",),
            dockerfile=Path("runtimes/watchdog/Dockerfile"),
            context=Path("runtimes/watchdog"),
        ),
        *(
            _function_target(repo_root, function)
            for function in list_functions(repo_root)
            if function.example_dir is not None
        ),
    ]
    targets.sort(key=lambda target: target.name)
    _validate_targets(repo_root, targets)
    return tuple(targets)


def _function_target(repo_root: Path, function: FunctionDefinition) -> ImageTarget:
    if function.example_dir is None:
        raise ValueError(f"function has no source directory: {function.key}")
    source_dir = function.example_dir.resolve().relative_to(repo_root)
    prefix = {"exec": "bash", "java-lite": "java-lite"}.get(
        function.runtime, function.runtime
    )
    name = f"{prefix}-{function.family}"

    if function.runtime == "java":
        return ImageTarget(
            name=name,
            flavors=("jvm", "native"),
            dockerfile=source_dir / "Dockerfile",
            context=source_dir,
            native_build=NativeBuild(
                task=f":functions:java:{function.family}:nativeCompile",
                binary=Path(
                    f"functions/java/{function.family}"
                    f"/build/native/nativeCompile/{function.family}"
                ),
            ),
            jvm_prerequisite_arguments=(
                f":functions:java:{function.family}:bootJar",
            ),
        )
    return ImageTarget(
        name=name,
        flavors=("native",) if function.runtime == "java-lite" else ("default",),
        dockerfile=source_dir / "Dockerfile",
        context=Path("."),
    )


def _validate_targets(repo_root: Path, targets: Sequence[ImageTarget]) -> None:
    names = [target.name for target in targets]
    duplicates = sorted(name for name in set(names) if names.count(name) > 1)
    if duplicates:
        raise ValueError(f"duplicate image target: {', '.join(duplicates)}")
    required = {target.dockerfile for target in targets}
    required.update(
        NATIVE_JAVA_DOCKERFILE for target in targets if target.native_build is not None
    )
    missing = sorted(
        path.as_posix() for path in required if not (repo_root / path).is_file()
    )
    if missing:
        raise FileNotFoundError(f"missing image Dockerfile: {', '.join(missing)}")


def _select_targets(
    targets: tuple[ImageTarget, ...], selectors: Sequence[str]
) -> tuple[ImageTarget, ...]:
    if not selectors:
        return targets
    requested = frozenset(selectors)
    known = {target.name for target in targets}
    unknown = sorted(requested - known)
    if unknown:
        raise ValueError(f"unknown image target: {', '.join(unknown)}")
    return tuple(target for target in targets if target.name in requested)


def _cell(
    target: ImageTarget,
    architecture: ImageArchitecture,
    flavor: ImageFlavor,
    version_tag: str,
    registry: str,
) -> ImageCell:
    tag = (
        f"{version_tag}-{architecture}"
        if flavor == "default"
        else f"{version_tag}-{architecture}-{flavor}"
    )
    return ImageCell(
        target=target,
        architecture=architecture,
        flavor=flavor,
        tag=tag,
        image=f"{registry}/{target.name}:{tag}",
    )
