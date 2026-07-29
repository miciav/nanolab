# Validate Docker Compose Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build and deploy the container validation control plane through nanoFaaS's canonical Docker Compose file.

**Architecture:** Add one reusable Sonata resource that owns `docker compose up --build` and `docker compose down`. The nanolab validate plan supplies that resource to the shared platform workflow, disables the redundant standalone control-plane build, and uses Compose's published API endpoint.

**Tech Stack:** Python, Sonata, Docker Compose, pytest.

---

### Task 1: Docker Compose lifecycle resource

**Files:**
- Create: `packages/sonata-tasks/src/sonata_tasks/compose.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/__init__.py`
- Test: `packages/sonata-tasks/tests/test_compose.py`

1. Write tests asserting the exact `up -d --build` and idempotent `down --remove-orphans` commands.
2. Run the tests and verify they fail because the resource does not exist.
3. Implement the two command tasks and compensated resource.
4. Run the tests and verify they pass.

### Task 2: Compose-backed validate plan

**Files:**
- Modify: `packages/sonata-tasks/src/sonata_tasks/platform.py`
- Modify: `packages/nanolab/src/nanolab/plans/validate.py`
- Modify: `packages/nanolab/tests/plans/test_validate.py`

1. Change the plan test to require Compose acquire/release, no standalone control-plane build, and port `8080`.
2. Run the test and verify the expected failure.
3. Add the smallest platform request switch needed to suppress only the redundant control-plane build.
4. Supply the Compose resource from the nanolab plan and update the endpoint.
5. Run targeted tests and compile both validate scenarios.

### Task 3: Live validation

1. Run the container workflow against Docker Desktop.
2. Confirm function registration, invocation, resource inspection, function removal, and Compose teardown.
3. Run the relevant package test suites and `git diff --check`.
