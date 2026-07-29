# Remove Dead Legacy Code — Design

## Scope

All user-facing scenario workflows now compile and run with Sonata. This cleanup
removes only code that is unreachable, superseded, or retained solely by tests.
It deliberately preserves the old runtime pieces still used by provisioning,
image builds, shell output routing, the TUI event bridge, VM providers, and the
load-test implementations reused by Sonata.

The removable surface is the four old workflow builders, their tests, the unused
legacy process resource, the deprecated `InstallK6` task, the dead scenario
runtime allowlist, and the always-true Sonata routing branches in the CLI and
TUI. Historical plans and active compatibility fixtures are not part of this
change.

## Structure

Two small pieces of useful data currently live inside otherwise dead workflow
modules. The resolved-function record moves into `nanolab.plans.validate`,
where all scenario plan builders already obtain it. The default Prometheus query
factory moves into `nanolab.plans.loadtest`, its only production consumer.
Nothing replaces the deleted workflow builders.

`uses_sonata()` and every alternative legacy rendering branch are removed.
Scenario plans are always compiled through Sonata, so CLI and TUI rendering read
only `workflow.compile().tasks`.

## Verification

Architecture tests will assert that production code no longer imports
`workflow_tasks.workflows` or `workflow_tasks.components.container`. Existing
behavioral tests continue to cover every Sonata scenario plan and the CLI/TUI
rendered task lists. The four package test suites, static type checks, Ruff,
import contracts, package builds, and representative plan generation must all
remain green.

