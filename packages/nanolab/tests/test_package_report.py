from __future__ import annotations

from nanolab.devtools.package_report import (
    PackageMetrics,
    calculate_metrics,
    format_metrics_table,
)


def test_calculate_metrics_counts_internal_outgoing_and_incoming_edges() -> None:
    metrics = calculate_metrics(
        packages=[
            "nanolab.core",
            "nanolab.plans",
            "nanolab.tui",
        ],
        edges=[
            ("nanolab.core.models", "nanolab.core.net_utils"),
            ("nanolab.plans.validate", "nanolab.core.models"),
            ("nanolab.tui.app", "nanolab.plans.validate"),
        ],
    )

    by_package = {metric.package: metric for metric in metrics}

    assert by_package["nanolab.core"] == PackageMetrics(
        package="nanolab.core",
        internal_imports=1,
        outgoing_imports=0,
        incoming_imports=1,
        instability=0.0,
    )
    assert by_package["nanolab.plans"] == PackageMetrics(
        package="nanolab.plans",
        internal_imports=0,
        outgoing_imports=1,
        incoming_imports=1,
        instability=0.5,
    )
    assert by_package["nanolab.tui"] == PackageMetrics(
        package="nanolab.tui",
        internal_imports=0,
        outgoing_imports=1,
        incoming_imports=0,
        instability=1.0,
    )


def test_format_metrics_table_includes_header_and_package_rows() -> None:
    table = format_metrics_table(
        [
            PackageMetrics(
                package="nanolab.core",
                internal_imports=1,
                outgoing_imports=0,
                incoming_imports=2,
                instability=0.0,
            )
        ]
    )

    assert "package" in table
    assert "internal" in table
    assert "outgoing" in table
    assert "incoming" in table
    assert "instability" in table
    assert "nanolab.core" in table
