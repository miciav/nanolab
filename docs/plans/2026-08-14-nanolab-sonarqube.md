# NanoLab Local SonarQube Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add and run an on-demand local SonarQube analysis for every Python package in the NanoLab workspace.

**Architecture:** Reuse the proven ephemeral-container lifecycle from nanoFaaS. A single shell script starts pinned SonarQube on localhost, scans the three workspace source and test trees with the host `sonar-scanner`, reports open issue counts, and optionally removes the container.

**Tech Stack:** Bash, Docker, SonarQube Community, sonar-scanner, Python standard library.

---

### Task 1: Local SonarQube script

**Files:**
- Create: `scripts/sonar.sh`
- Create: `packages/nanolab/tests/test_sonar_script.py`

1. Add a failing contract test proving the script uses the pinned local server, scans all workspace source/test directories, and replaces stale containers.
2. Run the focused test and confirm it fails because `scripts/sonar.sh` is absent.
3. Add the minimal script by adapting nanoFaaS `scripts/sonar.sh` to one Python project, `nanolab-python`.
4. Run the focused test and shell syntax check.
5. Run `scripts/sonar.sh --rm`, wait for Compute Engine completion, and record the reported findings.

