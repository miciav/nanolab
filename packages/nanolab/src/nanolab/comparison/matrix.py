"""The order in which the comparison's runs are executed, and where each lands.

The only interesting decision here is the order. Running all three repetitions of
one variant before moving to the next is the obvious layout and the wrong one: it
makes "which variant" and "when it ran" the same axis. An Azure host with a noisy
neighbour, a page cache that fills, a disk that slows as it fills — any drift over
the hour the matrix takes would land entirely on whichever variants ran late, and
the run would report it as a property of those builds.

Interleaving instead — every variant once, then every variant again — spreads each
build's three samples across the whole window, so a drift shows up as variance
within each variant rather than as a difference between them. The cost is eleven
extra control-plane redeploys, which is a helm upgrade and a pod restart apiece:
minutes against a run measured in hours, and it buys the difference between a
comparison and a coincidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from nanolab.images.control_plane_variants import ControlPlaneVariant


@dataclass(frozen=True, slots=True)
class ComparisonCell:
    """One (variant, repetition) pair: a single load-test run."""

    variant: ControlPlaneVariant
    repetition: int

    def run_dir(self, root: Path) -> Path:
        # Keyed by variant first so a half-finished matrix is still readable by
        # build, and by repetition second so the reader can see how many landed.
        return root / self.variant.key / f"run-{self.repetition}"

    @property
    def label(self) -> str:
        return f"{self.variant.key} run {self.repetition}"


def build_matrix(
    variants: tuple[ControlPlaneVariant, ...], repetitions: int
) -> tuple[ComparisonCell, ...]:
    """Every cell, ordered repetition-major so the variants interleave."""
    if repetitions < 1:
        raise ValueError("a comparison needs at least one repetition")
    if not variants:
        raise ValueError("a comparison needs at least one variant")
    return tuple(
        ComparisonCell(variant=variant, repetition=repetition)
        for repetition in range(1, repetitions + 1)
        for variant in variants
    )


def write_manifest(
    root: Path,
    cells: tuple[ComparisonCell, ...],
    *,
    functions: tuple[str, ...],
    registry: str,
) -> Path:
    """Record what was compared, so the report never has to infer it.

    Written before the first run rather than after the last: a matrix that dies
    halfway still says what it was attempting, which is the difference between a
    partial result and an unreadable directory.
    """
    variants: list[dict[str, object]] = []
    seen: set[str] = set()
    for cell in cells:
        if cell.variant.key in seen:
            continue
        seen.add(cell.variant.key)
        variants.append(
            {
                "key": cell.variant.key,
                "label": cell.variant.label,
                "rationale": cell.variant.rationale,
                "build_env": dict(cell.variant.build_env),
                "image": cell.variant.image(registry),
            }
        )
    manifest = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "functions": list(functions),
        "repetitions": max(cell.repetition for cell in cells),
        "order": [cell.label for cell in cells],
        "variants": variants,
    }
    root.mkdir(parents=True, exist_ok=True)
    path = root / "comparison-manifest.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path
