# NanoFaaS End-to-End Workflows in NanoLab

**Status:** Design specification
**Date:** 2026-07-31
**Implementation status:** Not implemented
**Target repository:** `miciav/nanolab`

## 1. Purpose

This document describes the end-to-end validation workflows that NanoLab should eventually provide for NanoFaaS.

The workflows described here are intentionally **not implemented as part of the current change**. The current change only establishes the ownership boundary and removes obsolete or misplaced E2E infrastructure from NanoFaaS.

This specification records the behavior currently covered by NanoFaaS E2E tests so that it can later be reimplemented as black-box NanoLab workflows.

## 2. Architectural decision

NanoFaaS should not own or expose end-to-end test orchestration.

NanoFaaS owns:

* unit tests;
* component tests;
* integration tests that do not require externally provisioned infrastructure;
* mock-based Kubernetes tests;
* build artifacts;
* container images;
* deployment manifests;
* public APIs consumed by external validation.

NanoLab owns:

* VM and cluster provisioning;
* Docker and Kubernetes setup;
* image preparation and registry management;
* Helm deployment;
* namespace lifecycle;
* black-box HTTP validation;
* Kubernetes resource inspection;
* metrics validation;
* diagnostics;
* cleanup.

The final architecture must not require Gradle to provision infrastructure or invoke system-level E2E scenarios.

## 3. Temporary coverage gap

Removing the Java E2E tests before their NanoLab replacements are implemented creates a deliberate temporary E2E coverage gap.

During this interval:

* NanoFaaS unit, component and integration tests remain active;
* NanoLab release and validation workflows continue to test the behavior they already cover;
* the scenarios described in this document are not yet guaranteed by an automated E2E gate.

This gap must remain visible in project documentation and release notes until the corresponding NanoLab workflows are implemented and validated.

The absence of E2E coverage must not be concealed by skipped Gradle tests or placeholder workflow tasks.

## 4. Current E2E behaviors to preserve

The following NanoFaaS Java tests contain behavior that should eventually be moved to NanoLab:

* `K8sE2eTest`
* `ContainerLocalE2eTest`
* `E2eFlowTest`
* `BuildpackE2eTest`
* `SdkExamplesE2eTest`

Before deleting each test, its externally observable assertions should be recorded in this specification or in a dedicated behavior inventory.

Internal implementation assertions that can be covered by ordinary NanoFaaS tests should remain in NanoFaaS rather than being migrated.

## 5. Planned NanoLab workflows

### 5.1 Container platform validation

**Proposed scenario name:**

```text
validate-container
```

**Purpose:** Validate a complete NanoFaaS control plane using the local container deployment provider.

The workflow should:

1. Build the NanoFaaS control-plane artifact.
2. Build the required function images.
3. Start the control plane.
4. Wait for its health and readiness endpoints.
5. Register one or more representative functions.
6. Verify the returned function metadata.
7. Invoke the functions synchronously.
8. Enqueue asynchronous executions.
9. Poll execution status until completion.
10. Validate idempotency behavior.
11. List registered functions.
12. Retrieve individual function metadata.
13. Delete the functions.
14. Verify that associated runtime resources are removed.
15. Collect logs and diagnostics.
16. Stop all resources created by the workflow.

This workflow should absorb the externally observable behavior currently covered by `ContainerLocalE2eTest` and the container-oriented portions of `E2eFlowTest`.

### 5.2 Kubernetes platform validation

**Proposed scenario name:**

```text
validate-k8s
```

**Purpose:** Validate NanoFaaS using the Kubernetes deployment provider on a real cluster.

The workflow should:

1. Provision or select a Kubernetes cluster.
2. Prepare a usable kubeconfig.
3. Create an isolated namespace.
4. Build and publish the control-plane image.
5. Build and publish representative function images.
6. Deploy NanoFaaS through Helm.
7. Enable the Kubernetes deployment provider.
8. Wait for the control-plane deployment and service.
9. Register representative functions.
10. Verify the provider-derived endpoint URL.
11. Wait for each generated function deployment.
12. Validate pod and deployment readiness.
13. Validate CPU and memory requests and limits.
14. Invoke functions synchronously.
15. Enqueue and poll asynchronous executions.
16. Verify idempotency.
17. Delete functions.
18. Verify that function deployments and services disappear.
19. Validate relevant Prometheus metrics.
20. Preserve Kubernetes diagnostics on failure.
21. Uninstall the Helm release and remove the namespace.

This workflow replaces the externally observable behavior of `K8sE2eTest`.

### 5.3 Queue and backpressure validation

**Proposed implementation:** A section of `validate-container` and `validate-k8s`, or a reusable workflow fragment.

The workflow should:

1. Register a function with bounded concurrency and queue depth.
2. Warm the function.
3. Submit a concurrent request burst.
4. Confirm that at least one request succeeds.
5. Confirm that excess requests receive HTTP `429`.
6. Validate `Retry-After`.
7. Validate `X-Queue-Reject-Reason`.
8. Validate queue admission, rejection, waiting-time and depth metrics.

The validation should run against every deployment backend for which queue semantics are expected to be equivalent.

### 5.4 Cold-start and warm-start metrics validation

**Proposed implementation:** A reusable validation fragment.

The workflow should:

1. Register a new function.
2. Invoke it once before a runtime instance exists.
3. Invoke it additional times after startup.
4. Retrieve Prometheus metrics.
5. Confirm at least one cold start.
6. Confirm at least one warm start.
7. Confirm that initialization-duration metrics are recorded.

Metrics should be validated through their public Prometheus representation rather than through internal Java objects.

### 5.5 Buildpack validation

**Proposed scenario name:**

```text
validate-buildpack
```

**Purpose:** Verify that supported NanoFaaS components can be built through the configured buildpack path and then executed successfully.

The workflow should:

1. Build the required artifact.
2. Build the OCI image using the supported buildpack mechanism.
3. Inspect the resulting image.
4. Start or deploy the image.
5. Wait for readiness.
6. Perform representative API or function invocations.
7. Preserve build logs and image metadata on failure.
8. Clean up generated images and runtime resources.

This workflow replaces the externally observable behavior currently covered by `BuildpackE2eTest`.

### 5.6 SDK examples validation

**Proposed scenario name:**

```text
validate-sdk-examples
```

**Purpose:** Verify that the supported SDK examples work against a running NanoFaaS deployment.

The workflow should:

1. Start or deploy a NanoFaaS control plane.
2. Build or prepare each supported SDK example.
3. Register representative functions through each SDK.
4. Invoke those functions.
5. Validate returned results.
6. Validate asynchronous execution where supported.
7. Remove registered functions.
8. Report results separately for each language SDK.

Initial coverage should include only SDKs that have stable example applications and deterministic build procedures.

This workflow replaces `SdkExamplesE2eTest`.

## 6. Representative function families

The workflows should use a small but meaningful set of functions.

Recommended families:

* echo or warm echo;
* word statistics;
* JSON transformation;
* Roman numeral conversion.

The selected set should cover:

* simple scalar input and output;
* structured JSON;
* collections;
* synchronous invocation;
* asynchronous invocation;
* configurable resource requirements.

The scenario catalog should define the selected functions, images, invocation payloads and expected output predicates.

## 7. Validation style

NanoLab should validate NanoFaaS as a black-box distributed system.

Preferred mechanisms:

* HTTP requests against public APIs;
* Helm status;
* Kubernetes resource queries;
* container runtime inspection;
* Prometheus metric parsing;
* observable logs and exit codes.

NanoLab should not import NanoFaaS Java classes or depend on NanoFaaS test fixtures unless the artifact is explicitly intended as a public machine-readable contract.

Assertions should focus on externally observable behavior rather than implementation details.

## 8. Diagnostics requirements

Every workflow must retain useful evidence when it fails.

Minimum common diagnostics:

* workflow journal;
* command stdout and stderr;
* control-plane logs;
* function runtime logs;
* health endpoint responses;
* failed HTTP responses;
* image metadata;
* scenario inputs.

Additional Kubernetes diagnostics:

* namespaces;
* pods;
* deployments;
* services;
* endpoints;
* events;
* resource descriptions;
* container logs;
* Helm release status.

Diagnostics must survive ordinary cleanup and must not replace the original failure status.

## 9. Cleanup requirements

Every workflow must define:

* resources created;
* cleanup order;
* behavior after successful completion;
* behavior after failure;
* behavior when resource preservation is requested.

Cleanup should be idempotent.

A `--keep` option, or its current NanoLab equivalent, should preserve the environment for manual investigation.

## 10. Future implementation order

Recommended implementation sequence:

1. `validate-container`
2. common API validation fragments
3. common metrics validation fragments
4. `validate-k8s`
5. queue and backpressure validation
6. cold-start and warm-start metrics validation
7. `validate-buildpack`
8. `validate-sdk-examples`

The container workflow should be implemented first because it requires less infrastructure and can establish the reusable API-validation abstractions.

## 11. Acceptance criteria for future implementation

A workflow is complete only when:

* it provisions all required dependencies;
* it executes real black-box operations;
* no expected validation is silently skipped;
* failures produce actionable diagnostics;
* cleanup is verified;
* focused automated tests cover workflow construction;
* at least one real execution has passed in a supported environment;
* active documentation contains the exact working command.

A successful plan compilation or dry run is not sufficient evidence of E2E success.

## 12. Out of scope for the current change

The following are explicitly deferred:

* implementing the workflows described above;
* adding a new E2E CI environment;
* selecting the final VM or cloud provider;
* implementing additional performance benchmarks;
* repairing unrelated release image-cell counts;
* modifying historical design documents;
* guaranteeing E2E coverage during the temporary migration interval.

## 13. Follow-up implementation plan

A separate implementation plan should be created only when work on the first workflow begins.

Suggested future plan path:

```text
docs/superpowers/plans/YYYY-MM-DD-nanofaas-container-e2e-implementation.md
```

That plan should derive concrete tasks from this specification and begin with `validate-container`.
