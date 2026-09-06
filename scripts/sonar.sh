#!/usr/bin/env bash
# Local SonarQube analysis for the NanoLab Python workspace.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

SONAR_HOST="http://127.0.0.1:9000"
SONAR_IMAGE="sonarqube:26.7.0.124771-community"
CONTAINER_NAME="sonar-nanolab"
PROJECT_KEY="nanolab-python"
KEEP=true
DRY=false

usage() {
    cat <<'EOF'
Usage: scripts/sonar.sh [--rm] [--dry-run]

Runs local SonarQube analysis for every Python package in the NanoLab workspace.

  --rm       remove the SonarQube container when the run finishes
  --dry-run  print scanner commands without starting SonarQube
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --rm) KEEP=false; shift ;;
        --dry-run) DRY=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 1 ;;
    esac
done

command -v docker >/dev/null || { echo "docker not found on PATH" >&2; exit 1; }
command -v sonar-scanner >/dev/null || {
    echo "sonar-scanner not found on PATH. Install it: brew install sonar-scanner" >&2
    exit 1
}
command -v python3 >/dev/null || { echo "python3 not found on PATH" >&2; exit 1; }

cleanup() {
    if [ "$KEEP" = false ] && [ "$DRY" = false ]; then
        docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

TOKEN=""
if [ "$DRY" = false ]; then
    docker info >/dev/null 2>&1 || { echo "Docker daemon not reachable" >&2; exit 1; }
    if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
        docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    fi
    if lsof -iTCP:9000 -sTCP:LISTEN >/dev/null 2>&1; then
        echo "Port 9000 is already in use" >&2
        exit 1
    fi
    docker run -d --name "$CONTAINER_NAME" -p 127.0.0.1:9000:9000 "$SONAR_IMAGE" >/dev/null

    echo "Waiting for SonarQube on ${SONAR_HOST}..."
    deadline=$((SECONDS + 300))
    until curl -sf "$SONAR_HOST/api/system/status" 2>/dev/null | python3 -c \
        'import json,sys; sys.exit(json.load(sys.stdin).get("status") != "UP")' 2>/dev/null; do
        if (( SECONDS >= deadline )); then
            docker logs --tail 50 "$CONTAINER_NAME" >&2 || true
            echo "SonarQube did not become ready within 300 seconds" >&2
            exit 1
        fi
        sleep 5
    done

    token_name="nanolab-run-$(date +%s)"
    if ! TOKEN="$(curl -sf -u admin:admin -X POST "$SONAR_HOST/api/user_tokens/generate" \
        -d "name=${token_name}" -d type=USER_TOKEN | python3 -c \
        'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null)"; then
        new_password="Nanolab$(date +%s)!"
        curl -sf -u admin:admin -X POST "$SONAR_HOST/api/users/change_password" \
            -d login=admin -d previousPassword=admin -d "password=${new_password}" >/dev/null
        TOKEN="$(curl -sf -u "admin:${new_password}" -X POST \
            "$SONAR_HOST/api/user_tokens/generate" -d "name=${token_name}" \
            -d type=USER_TOKEN | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"
    fi
fi

scanner=(
    sonar-scanner
    -Dsonar.host.url="$SONAR_HOST"
    -Dsonar.token="$TOKEN"
    -Dsonar.projectKey=nanolab-python
    -Dsonar.projectName="NanoLab Python"
    -Dsonar.python.version=3.12
    -Dsonar.sources=packages/nanolab/src,packages/tui-toolkit/src
    -Dsonar.tests=packages/nanolab/tests,packages/tui-toolkit/tests
    -Dsonar.exclusions="**/__pycache__/**,**/*.pyc"
    -Dsonar.sourceEncoding=UTF-8
)

if [ "$DRY" = true ]; then
    printf '$'; printf ' %q' "${scanner[@]}"; printf '\n'
    exit 0
fi

"${scanner[@]}"

echo "Waiting for SonarQube analysis..."
deadline=$((SECONDS + 180))
while true; do
    status="$(curl -sf -u "$TOKEN": "$SONAR_HOST/api/ce/component?component=${PROJECT_KEY}" \
        | python3 -c 'import json,sys; print((json.load(sys.stdin).get("current") or {}).get("status", ""))')"
    case "$status" in
        SUCCESS) break ;;
        FAILED|CANCELED) echo "SonarQube analysis finished with status ${status}" >&2; exit 1 ;;
    esac
    if (( SECONDS >= deadline )); then
        echo "SonarQube analysis did not finish within 180 seconds" >&2
        exit 1
    fi
    sleep 2
done

mkdir -p .scannerwork
curl -sf -u "$TOKEN": \
    "$SONAR_HOST/api/issues/search?componentKeys=${PROJECT_KEY}&resolved=false&ps=500&facets=impactSeverities" \
    | tee .scannerwork/issues.json \
    | python3 -c '
import json, sys

data = json.load(sys.stdin)
counts = {}
for facet in data.get("facets", []):
    if facet.get("property") == "impactSeverities":
        counts = {item["val"]: item["count"] for item in facet["values"]}
total = data.get("paging", {}).get("total", 0)
print(f"NanoLab Python: {total} open issues " + " ".join(
    f"{severity}={counts.get(severity, 0)}"
    for severity in ("BLOCKER", "HIGH", "MEDIUM", "LOW", "INFO")
))
'
echo "Detailed findings: .scannerwork/issues.json"

if [ "$KEEP" = true ]; then
    echo "SonarQube UI: ${SONAR_HOST}/project/issues?resolved=false&id=${PROJECT_KEY}"
fi
