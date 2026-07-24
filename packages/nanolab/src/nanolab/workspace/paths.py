from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


_NANOFAAS_MARKERS = ("build.gradle", "settings.gradle")


@dataclass(frozen=True)
class ToolPaths:
    nanofaas_root: Path
    tool_root: Path
    profiles_dir: Path
    runs_dir: Path
    scenarios_dir: Path
    scenario_payloads_dir: Path

    @classmethod
    def from_roots(cls, nanofaas_root: Path, tool_root: Path) -> "ToolPaths":
        source_root = Path(nanofaas_root)
        product_root = Path(tool_root)
        return cls(
            nanofaas_root=source_root,
            tool_root=product_root,
            profiles_dir=product_root / "profiles",
            runs_dir=product_root / "runs",
            scenarios_dir=product_root / "scenarios",
            scenario_payloads_dir=product_root / "scenarios" / "payloads",
        )


def discover_tool_root() -> Path:
    return Path(__file__).resolve().parents[3]


def nanofaas_root_from_env() -> Path:
    value = os.getenv("NANOFAAS_ROOT", "").strip()
    if not value:
        raise RuntimeError("NANOFAAS_ROOT must point to a nanoFaaS checkout")
    root = Path(value).expanduser().resolve()
    missing = [marker for marker in _NANOFAAS_MARKERS if not (root / marker).is_file()]
    if missing:
        raise RuntimeError(
            f"NANOFAAS_ROOT is not a nanoFaaS checkout; missing: {', '.join(missing)}"
        )
    return root


def default_tool_paths() -> ToolPaths:
    return ToolPaths.from_roots(nanofaas_root_from_env(), discover_tool_root())


def scenario_path_from_env(cli_path: Path | None = None) -> Path | None:
    """Resolve scenario path: CLI argument takes precedence over NANOFAAS_SCENARIO_PATH env var."""
    if cli_path is not None:
        return cli_path

    s = os.getenv("NANOFAAS_SCENARIO_PATH", "").strip()
    return Path(s) if s else None
