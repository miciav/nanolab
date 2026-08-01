# Sonata Release Migration Design

## Objective

Make `nanolab run scenarios-v2/release.yaml` the only release execution path. The
Sonata workflow must preserve every safety property of the current procedural
runner before `nanolab release run` and its private journal/orchestrator are
removed.

`nanolab release prepare` remains: preparing and committing a version is a local
source operation, not workflow execution. The separate `nanolab release plan` and
`nanolab release run` commands disappear at cutover; generic `nanolab plan` and
`nanolab run` become the only plan/run surface.

## Chosen architecture

The workflow is a linear release DAG with Sonata resources around the phases that
need them. It uses one task per semantic release phase rather than exposing every
shell command as a top-level task. This keeps resume and evidence aligned with the
meaningful failure boundary: source tests, AMD64 build, registry push, each
benchmark, aggregate, gate, ARM64 build, ARM64 smoke, publish architectures,
publish manifests, publish aliases, attest, and finalize.

The existing procedural implementation is treated as a tested reference, not as
a second runtime. Its pure planning and execution helpers move out of
`release/run.py` into small domain modules and are called by Sonata tasks. Sonata
owns ordering, resource lifetime, cleanup, selection, journaling, and resume.

Release-specific code stays in `nanolab.release`; `sonata_tasks` receives only
generic reusable resources or tasks. This avoids teaching the shared task catalog
about nanoFaaS version policy, GHCR naming, Azure performance profiles, or release
records.

## Resource lifecycle

Three Sonata infrastructure resources own the stack, load-generator, and ARM64
VMs. Their acquire functions ensure the VM exists, bootstrap it, verify its pinned
Azure facts, and apply bounded NSG rules. Their release functions destroy all VM
resources. `--keep` retains only these infrastructure resources.

Non-infrastructure resources stage source archives, Bake/BuildKit inputs, the
registry tunnel, GHCR credentials, Cosign credentials, and temporary builders.
They always clean up, including when `--keep` is set or a later phase fails.
Credential resources validate local ownership/mode before provisioning, transfer
secrets only immediately before their consumers, and remove them in reverse
order.

No provider call occurs while compiling a plan. Hosts and endpoints are resolved
from acquired VM resources at execution time, so `nanolab plan` remains offline.

## Evidence and resume

The Sonata JSONL journal is the only execution journal. Every reusable release
phase is a `ReusableTask` whose reuse key contains the source commit, prepared
version, scenario digest, environment digest, release policy, image matrix, and
phase-specific inputs. Each successful task returns evidence:

- `file-digest` for local summaries, aggregates, decisions, predicates and records;
- `remote-image-digest` for local-registry and GHCR images;
- no evidence means no reuse.

Injected verifiers re-read the current file or registry digest. Unknown evidence
kinds, unreachable registries, missing credentials, changed topology, changed
configuration, or changed source all fail closed and rerun from the first unsafe
phase. A release-wide local lock prevents concurrent runs against the same Azure
VM identity.

## Correctness gates

Benchmarks consume the exact AMD64 native images built by the release, pinned by
digest. They never rebuild `:e2e` images. The aggregate reads only the current
run's verified summaries. The regression gate writes its decision and raises on
failure; publication cannot be reached without verified passing gate and ARM64
smoke evidence.

ARM64 receives the same committed source archive and generated build inputs as
AMD64. Its images are pushed through the temporary registry tunnel and verified
in the stack registry before publication. Publication copies immutable
architecture tags by verified digest, verifies manifests, and only then updates
mutable aliases. Attestation signs published digests, verifies the attestations,
and finalizes performance documentation atomically as the last phase.

## Cutover rule

The legacy command is removed only after contract tests run the Sonata workflow
through every phase with a fake provider, failure-injection tests prove cleanup
and publication barriers, resume tests prove evidence invalidation, and one Azure
canary passes. Until then, the incomplete generic release execution fails closed
rather than publishing.

