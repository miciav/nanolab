# SonarQube Issues Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Resolve the sixteen SonarQube findings without changing supported scenario behaviour.

**Architecture:** Extract small pure helpers from the flagged orchestration functions, retaining their current public API. Correct the four concrete defects in their source functions and prove each with the existing focused test suites.

**Tech Stack:** Python 3.12, pytest, Ruff, basedpyright, SonarQube.

---

### Task 1: Diagnose each finding and add focused regression tests

**Files:**
- Modify: the existing tests adjacent to each of the thirteen flagged modules.

**Step 1:** Add a failing test only for the four correctness findings (tuple arity, float comparison, repeated comparison expression, and Docker UDS scheme).

**Step 2:** Run the focused tests and verify each fails for the reported condition.

### Task 2: Apply minimal source corrections

**Files:**
- Modify: the thirteen files listed in `.scannerwork/issues.json`.

**Step 1:** Correct the four defects with standard-library constructs.

**Step 2:** Extract private helpers from the twelve complexity/duplication/unused-parameter findings; do not alter public function signatures except the unused private parameter.

**Step 3:** Run the focused test modules and verify they pass.

### Task 3: Verify analysis result

**Step 1:** Run all tests, Ruff, and basedpyright.

**Step 2:** Run `./scripts/sonar.sh` and verify the issue count is zero.

**Step 3:** Commit with a short imperative message.
