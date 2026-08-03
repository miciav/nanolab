# Release workflow remediation — carried follow-ups

Deferred items from executing `2026-08-03-release-workflow-remediation.md`. The final
whole-branch review triaged every one of these as safe to carry into main; none is a latent
correctness bug on a reachable path. Recorded here because they were consciously deferred,
not overlooked.

## Worth doing next

**Attest receipt under-claims after a resume.** A resumed release's `attest.json` records only
the digests that actually ran; groups skipped on verified evidence contribute nothing, because
`Steps.run` never sees the evidence behind a skip. This contradicts the plan's own Prova 1
criterion, which expects `1 + len(unique published digests)` entries. It fails safe — nothing
downstream counts signatures, and `require_attestation_predicate` reads only the predicate's
`file-digest`. Upgrade: emit one `cosign-attestation` per entry of `pinned` rather than per
group that ran. That is not over-claiming: `run_steps` raises on any failure, so a composite
that returns means every group either completed or was skipped on evidence the journal verified.

**The attestation reuse key says nothing about which key signed.** `_AttestImageTask`'s identity
deliberately excludes the staged key path (that exclusion is load-bearing — the path is a fresh
`mktemp -d` per process, and folding it in defeated the skip entirely). But it carries nothing
about the key itself, so rotating the cosign key and resuming the same journal would skip groups
signed by the old key, shipping mixed-provenance signatures. Fail-open, low probability, high
blast radius. Upgrade: fold a digest of the *local* key file, never the staged path.

**Three unreachable flags on `_build_arm64_images` / `_smoke_arm64_images`**
(`release/build.py`): `stage_inputs`, `manage_resources`, `ensure_tunnel`. Every caller and every
test passes `False`. The `True` branches keep `_reset_named_builder` and the `render_bake_json`
import alive as dead code, and the defaults would `buildx rm --force` a builder the Sonata
resource owns and open a second tunnel. ~45 lines. Deferred because it is a zero-behaviour-change
edit to a path with no canary since the runner deletion — fold it into the next change that
touches ARM64 anyway.

## Small and mechanical

- **Public-key prelude step's `idempotent=True` is inert.** `CosignTask` defaults its title to
  `f"cosign {operation} {image or key_file}"`, and `key_file` is a per-process `mktemp -d` path,
  so the journal slug moves every run and one dead id accrues per run. Fix: pass an explicit
  `title="Derive cosign public key"` (`plans/release.py`).
- **Dead phase-name constants:** `ATTEST_PHASES` (`release/attest.py`), `ARM64_PHASES`
  (`release/arm.py`), `PUBLISH_PHASES` (`release/publish.py`). Their only consumer was
  `RELEASE_PHASES` in the deleted `run.py`.
- **Staged filenames are spelled independently** in `plans/release.py` and `release/resources.py`
  with nothing asserting they agree; a rename passes the whole suite and fails on hardware. The
  resource cannot hand the path back — `buildx_builder_resource` needs it at construction — so
  the fix is a shared name helper.
- **`attest_composite` validates only `"@" in image`**, not the digest shape, while its docstring
  promises digest-pinning. Production is safe (`_pinned` builds from `exact_receipt_artifacts`,
  which enforces `is_sha256_digest`).
- **`_build_arm64_images`' invalid-digest guard lost its ARM-path test** when the procedural
  runner's tests went. The registry-digest twin is still covered at DAG level.
- **Dead fixture state:** `remote_source_mutated` in `tests/release/_release_support.py` is read
  but never set `True`.

## Not a code issue

The release test suite needs `NANOFAAS_ROOT` to point at a **clean** git tree.
`test_build_release_request_requires_credentials_for_execution` reaches `git_state` for real and
fails with `release requires a clean nanoFaaS Git tree` whenever the checkout is dirty, long
before its actual assertion.
