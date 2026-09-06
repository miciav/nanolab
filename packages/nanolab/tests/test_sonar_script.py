from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_sonar_script_scans_every_workspace_package() -> None:
    script = (REPO_ROOT / "scripts/sonar.sh").read_text(encoding="utf-8")

    assert 'SONAR_IMAGE="sonarqube:26.7.0.124771-community"' in script
    assert 'SONAR_HOST="http://127.0.0.1:9000"' in script
    assert "-Dsonar.projectKey=nanolab-python" in script
    assert (
        "-Dsonar.sources=packages/nanolab/src,packages/tui-toolkit/src"
        in script
    )
    assert (
        "-Dsonar.tests=packages/nanolab/tests,packages/tui-toolkit/tests"
        in script
    )
    assert 'docker rm -f "$CONTAINER_NAME"' in script


def test_sonar_script_treats_startup_responses_as_quiet_retries() -> None:
    script = (REPO_ROOT / "scripts/sonar.sh").read_text(encoding="utf-8")

    readiness = script.split("Waiting for SonarQube", 1)[1].split("token_name=", 1)[0]
    assert "python3 -c" in readiness
    assert "2>/dev/null" in readiness.split("python3 -c", 1)[1]


def test_sonar_script_sets_the_workspace_python_version() -> None:
    script = (REPO_ROOT / "scripts/sonar.sh").read_text(encoding="utf-8")

    assert "-Dsonar.python.version=3.12" in script


def test_sonar_working_directory_is_ignored() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".scannerwork/" in gitignore


def test_sonar_script_exports_detailed_findings_without_credentials() -> None:
    script = (REPO_ROOT / "scripts/sonar.sh").read_text(encoding="utf-8")

    assert "ps=500" in script
    assert '.scannerwork/issues.json' in script
