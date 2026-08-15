# Validate Async Payloads Container Implementation Plan

**Goal:** Add a container `validate` workload that compiles the control plane (JVM, via the existing Compose build), compiles every function across all SDKs, deploys, generates **async** load (`:enqueue` + execution polling) for every function, and verifies each function received and correctly processed its payloads against per-function payload sets. Reuse the existing platform/invoke machinery as much as possible.

**Architecture:** Keep `workflow: validate`, `backend: container`, and the existing registry/Compose/platform resources. Enable the async-queue modules on the Compose control plane (same env the container loadtest already passes), and add one async enqueue→poll→verify composite per function payload. Payloads are the per-function `functions/<runtime>/<family>/payloads/*.json` files (`{description, input, expected}`) already used by the nanoFaaS function-test convention; missing ones are created in nanoFaaS from the authoritative `functions/test-data/<family>/correctness.json`.

**Scope:** word-stats, json-transform, and roman-numeral across all six runtimes (18 functions) — the three JSON-output reference families that span every SDK. qr-code (binary envelope) and the handler/binary-envelope test functions stay in the existing `handler-envelope-container` scenario, which already asserts their base64/header contracts.

---

## Contract

For each selected function, for each payload file in its `payloads/` directory:
- POST `{"input": <payload.input>}` to `/v1/functions/{name}:enqueue`, expect 202 with an `executionId`.
- Poll `GET /v1/executions/{id}` until `status == "success"`, then assert `output == <payload.expected>`.

The control plane for a container `validate` with async load must be built with modules `container-deployment-provider,async-queue,sync-queue` (the same set the container loadtest passes).

### Task 1: Verify async execution output

**Files:**
- Modify: `packages/sonata-tasks/src/sonata_tasks/http_function.py`
- Test: `packages/sonata-tasks/tests/test_invocation.py`

Extend `HttpExecutionSuccessTask` with optional `expected_output` and `expected_status_code`. On the polled `status == "success"` response, assert `output`/`statusCode` match when provided; default `None` keeps the existing callers unchanged. TDD: add failing tests for a matching output, a wrong output, and a wrong status.

### Task 2: Compose async checks into the validate workflow

**Files:**
- Modify: `packages/sonata-tasks/src/sonata_tasks/validate.py`
- Test: `packages/sonata-tasks/tests/test_validate_workflow.py`

Add an `AsyncCheck` frozen dataclass (`function_name`, `payload`, `expected_output`, `payload_name`) and an empty-by-default `async_checks` tuple on `ValidateWorkflowRequest`. In `build_validate_workflow`, add an enqueue→poll composite per check, gated on the function's registration. TDD: assert the composite compiles after registration and runs the enqueue→executions cycle.

### Task 3: Wire async payload checks into the container plan

**Files:**
- Modify: `packages/nanolab/src/nanolab/config/scenario.py`
- Modify: `packages/nanolab/src/nanolab/plans/functions.py`
- Modify: `packages/nanolab/src/nanolab/plans/validate.py`
- Create: `packages/nanolab/scenarios-v2/validate-async-container.yaml`
- Test: `packages/nanolab/tests/plans/test_validate.py`

Add `async_load: bool` (alias `asyncLoad`) to `ScenarioConfig`. Add `resolve_function_payloads(key, source_root)` reading `functions/<runtime>/<family>/payloads/*.json` (empty when the directory is absent). In `build_validate_plan`, when `async_load` is set, pass the async modules to the Compose resource and build `async_checks` for every selected function payload. TDD: assert the Compose env carries the async modules and one async composite compiles per payload.

### Task 4: Provide the payload sets in nanoFaaS

**Files (nanoFaaS checkout):**
- Create/extend `functions/<runtime>/<family>/payloads/{happy-path.json,missing-input.json}` for word-stats and json-transform runtimes that lack them, and fix the roman-numeral stubs (go/bash/python/java-lite/javascript) to real contracts.

Derive `happy-path.json` from the first valid case and `missing-input.json` from the missing-input error case in `functions/test-data/<family>/correctness.json`, preserving the `{description, input, expected}` schema and the 2-file cardinality of the other functions.

### Task 5: Verify

Run the static gate for both packages, then execute the real workflow:

```bash
NANOFAAS_ROOT=/path/to/nanofaas uv run --locked --package nanolab nanolab run packages/nanolab/scenarios-v2/validate-async-container.yaml
```

Expected: every async check passes and Sonata release events remove functions, the Compose project, and the registry.
