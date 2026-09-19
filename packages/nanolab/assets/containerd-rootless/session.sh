#!/usr/bin/env bash
# One NanoLab run owns one containerd namespace, one registry, one user unit,
# and only the RootlessKit port IDs recorded below.
set -euo pipefail

action=${1:?action required}
run_id=${2:?run id required}
repo_root=${3:?absolute repository path required}
shift 3

[[ $run_id =~ ^[a-z0-9-]+$ ]] || { echo "invalid run id" >&2; exit 2; }
[[ $repo_root =~ ^/[A-Za-z0-9_./-]+$ ]] || {
  echo "repository path must be absolute and contain only letters, digits, _, -, . and /" >&2
  exit 2
}
real_home=$(getent passwd "$(id -u)" | cut -d: -f6)
[[ -n $real_home && $real_home == /* ]] || { echo "cannot resolve user home" >&2; exit 2; }
export HOME=$real_home XDG_RUNTIME_DIR=/run/user/$(id -u)
export CONTAINERD_ADDRESS=$XDG_RUNTIME_DIR/containerd/containerd.sock
export CONTAINERD_SNAPSHOTTER=native
state=$HOME/.local/share/nanolab/containerd-rootless/$run_id
rk_socket=$XDG_RUNTIME_DIR/containerd-rootless/api.sock
unit_name=nanofaas-$run_id.service
unit_path=$HOME/.config/systemd/user/$unit_name
namespace=nanofaas-$run_id
registry_namespace=nanolab-registry-$run_id
registry_name=nanolab-registry-$run_id
prometheus_name=nanolab-prometheus-$run_id

port_add() {
  local name=$1 mapping=$2 id
  id=$(rootlessctl --socket "$rk_socket" add-ports "$mapping")
  [[ $id =~ ^[0-9]+$ ]] || { echo "rootlessctl gave no port ID: $id" >&2; exit 1; }
  printf '%s\n' "$id" > "$state/port-$name"
}

port_remove() {
  local name=$1
  local id_file=$state/port-$name
  [[ -f $id_file ]] || return 0
  local id ports
  id=$(cat "$id_file")
  ports=$(rootlessctl --socket "$rk_socket" list-ports) || return 1
  if awk -v id="$id" '$1 == id { found = 1 } END { exit !found }' <<< "$ports"; then
    rootlessctl --socket "$rk_socket" remove-ports "$id"
  fi
  rm -f "$id_file"
}

registry_stop() {
  local containers
  if [[ -f $state/registry-owned ]]; then
    containers=$(nerdctl --namespace "$registry_namespace" ps -aq --filter "name=$registry_name") || return 1
    if [[ -n $containers ]]; then
      nerdctl --namespace "$registry_namespace" rm -f "$registry_name" || return 1
    fi
    rm -f "$state/registry-owned"
  fi
  port_remove registry
}

control_stop() {
  if [[ -f $unit_path ]]; then
    systemctl --user stop "$unit_name" || return 1
  fi
  port_remove management
  port_remove api
  if [[ -f $unit_path ]]; then
    rm -f "$unit_path"
    systemctl --user daemon-reload
  fi
  if [[ -f $state/soak-limits-owned ]]; then
    rm -f "$unit_path.d/limits.conf" "$state/soak-limits-owned"
    rmdir "$unit_path.d" 2>/dev/null || true
    systemctl --user daemon-reload
  fi
  rm -f "$state/control-plane.env"
}

prometheus_stop() {
  local containers
  if [[ -f $state/prometheus-owned ]]; then
    containers=$(nerdctl --namespace "$registry_namespace" ps -aq --filter "name=$prometheus_name") || return 1
    if [[ -n $containers ]]; then
      nerdctl --namespace "$registry_namespace" rm -f "$prometheus_name" || return 1
    fi
    rm -f "$state/prometheus-owned"
  fi
  port_remove prometheus
  rm -f "$state/prometheus.yml"
}

case $action in
  registry-start)
    bash "$(dirname "$0")/provision.sh" check
    mkdir -p "$state"
    chmod 700 "$state"
    trap 'registry_stop' ERR
    [[ ! -f $state/registry-owned && ! -f $state/port-registry ]] || {
      echo "registry already owned by run $run_id" >&2; exit 1;
    }
    touch "$state/registry-owned"
    nerdctl --namespace "$registry_namespace" run -d \
      --name "$registry_name" --net host --runtime crun \
      docker.io/library/registry:2 >/dev/null
    port_add registry 127.0.0.1:5000:5000/tcp
    curl -fsS --max-time 5 --retry 20 --retry-all-errors --retry-delay 1 --retry-max-time 30 \
      http://127.0.0.1:5000/v2/ >/dev/null
    trap - ERR
    ;;
  registry-stop)
    registry_stop
    ;;
  prometheus-start)
    bash "$(dirname "$0")/provision.sh" check
    mkdir -p "$state"
    chmod 700 "$state"
    trap 'prometheus_stop' ERR
    [[ ! -f $state/prometheus-owned && ! -f $state/port-prometheus ]] || {
      echo "Prometheus already owned by run $run_id" >&2; exit 1;
    }
    cat > "$state/prometheus.yml" <<'PROMETHEUS'
global:
  scrape_interval: 1s
scrape_configs:
  - job_name: nanofaas
    metrics_path: /actuator/prometheus
    static_configs:
      - targets: ["127.0.0.1:8081"]
PROMETHEUS
    touch "$state/prometheus-owned"
    nerdctl --namespace "$registry_namespace" run -d \
      --name "$prometheus_name" --net host --runtime crun \
      -v "$state/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
      docker.io/prom/prometheus:v3.5.1 >/dev/null
    port_add prometheus 0.0.0.0:9090:9090/tcp
    curl -fsS --max-time 5 --retry 30 --retry-all-errors --retry-delay 1 --retry-max-time 30 \
      http://127.0.0.1:9090/-/ready >/dev/null
    trap - ERR
    ;;
  prometheus-stop)
    prometheus_stop
    ;;
  control-start)
    cpuset_cores=${1:-0}
    budget=${2:-}
    artifact_mode=${3:-}
    artifact_path=${4:-}
    cpu_limit=${5:-}
    memory_limit=${6:-}
    [[ $cpuset_cores =~ ^[0-9]+$ && $budget =~ ^[0-9]*$ ]] || {
      echo "core count and budget must be nonnegative integers" >&2; exit 2;
    }
    if [[ -n $artifact_mode || -n $artifact_path || -n $cpu_limit || -n $memory_limit ]]; then
      [[ $artifact_mode == jvm || $artifact_mode == native ]] || {
        echo "invalid control-plane artifact mode" >&2; exit 2;
      }
      [[ $artifact_path =~ ^/[A-Za-z0-9_./-]+$ && $artifact_path == "$repo_root/"* && -f $artifact_path ]] || {
        echo "control-plane artifact must be a built file in the staged repository" >&2; exit 2;
      }
      [[ $cpu_limit =~ ^[0-9]+([.][0-9]+)?$ && $memory_limit =~ ^[0-9]+$ && $memory_limit -gt 0 ]] || {
        echo "invalid soak CPU or memory limit" >&2; exit 2;
      }
    fi
    bash "$(dirname "$0")/provision.sh" check
    mkdir -p "$state/containerd" "$state/cni-cache" "$HOME/.config/systemd/user"
    chmod 700 "$state" "$state/containerd" "$state/cni-cache"
    trap 'control_stop' ERR
    [[ ! -f $unit_path ]] || { echo "unit already owned by run $run_id" >&2; exit 1; }
    env_file=$state/control-plane.env
    printf 'NANOFAAS_ROOT=%s\nHOME=%s\nXDG_RUNTIME_DIR=%s\nSERVER_ADDRESS=0.0.0.0\nSERVER_PORT=8080\nMANAGEMENT_SERVER_PORT=8081\nNANOFAAS_DEPLOYMENT_DEFAULTBACKEND=containerd\nNANOFAAS_CONTAINERD_SOCKETPATH=%s\nNANOFAAS_CONTAINERD_NAMESPACE=%s\nNANOFAAS_CONTAINERD_NETWORKNAME=nanofaas\nNANOFAAS_CONTAINERD_RUNTIMEBINARY=crun\nNANOFAAS_CONTAINERD_SNAPSHOTTER=native\nNANOFAAS_CONTAINERD_CNIPLUGINDIRECTORY=/usr/local/libexec/cni\nNANOFAAS_CONTAINERD_CNICONFIGDIRECTORY=%s\nNANOFAAS_CONTAINERD_CNICACHEDIRECTORY=%s\nNANOFAAS_CONTAINERD_STATEDIRECTORY=%s\nNANOFAAS_CONTAINERD_CALLBACKURL=http://10.90.0.1:8080\nNANOFAAS_CONTAINERD_BINDHOST=127.0.0.1\n' \
      "$repo_root" "$HOME" "$XDG_RUNTIME_DIR" "$CONTAINERD_ADDRESS" \
      "$namespace" "$HOME/.config/cni/net.d" \
      "$state/cni-cache" "$state/containerd" > "$env_file"
    printf 'NANOFAAS_REGISTRY_PATH=%s\n' "$state/functions.json" >> "$env_file"
    if [[ -n $artifact_mode ]]; then
      printf 'NANOFAAS_CONTROL_PLANE_MODE=%s\nNANOFAAS_CONTROL_PLANE_ARTIFACT=%s\n' \
        "$artifact_mode" "$artifact_path" >> "$env_file"
      [[ ! -e $unit_path.d/limits.conf ]] || {
        echo "soak unit limits already exist" >&2; exit 1;
      }
      mkdir -p "$unit_path.d"
      touch "$state/soak-limits-owned"
      cpu_percent=$(python3 - "$cpu_limit" <<'PY'
from decimal import Decimal
import sys
print(Decimal(sys.argv[1]) * 100)
PY
      )
      printf '[Service]\nCPUQuota=%s%%\nMemoryMax=%s\n' \
        "$cpu_percent" "$memory_limit" > "$unit_path.d/limits.conf"
    fi
    if (( cpuset_cores > 0 )); then
      cpuset=$(python3 - "$cpuset_cores" <<'PY'
import os
import sys

count = int(sys.argv[1])
available = sorted(os.sched_getaffinity(0))
if len(available) < count:
    raise SystemExit(f"requested {count} shared CPUs, VM exposes {len(available)}")
print(",".join(str(cpu) for cpu in available[:count]))
PY
      )
      printf 'NANOFAAS_CONTAINERD_CPUSET=%s\n' "$cpuset" >> "$env_file"
    fi
    if [[ -n $budget ]]; then
      printf 'NANOFAAS_CONCURRENCYCONTROL_TOTALBUDGET=%s\n' "$budget" >> "$env_file"
    fi
    python3 - "$repo_root/deploy/containerd-rootless/nanofaas.service" "$unit_path" "$repo_root" "$env_file" <<'PY'
from pathlib import Path
import sys
source, target, repo_root, env_file = sys.argv[1:]
template = Path(source).read_text()
for marker in ('@NANOFAAS_ROOT@', '@NANOLAB_ENV_FILE@'):
    if marker not in template:
        raise SystemExit(f"missing service template marker {marker}")
Path(target).write_text(template.replace('@NANOFAAS_ROOT@', repo_root).replace('@NANOLAB_ENV_FILE@', env_file))
PY
    systemctl --user daemon-reload
    systemctl --user start "$unit_name"
    port_add api 0.0.0.0:8080:8080/tcp
    port_add management 0.0.0.0:8081:8081/tcp
    curl -fsS --max-time 5 --retry 30 --retry-all-errors --retry-delay 1 --retry-max-time 30 \
      http://127.0.0.1:8081/actuator/health/readiness >/dev/null
    trap - ERR
    ;;
  control-stop)
    control_stop
    ;;
  control-restart)
    [[ -f $unit_path ]] || { echo "no owned unit $unit_name" >&2; exit 1; }
    systemctl --user restart "$unit_name"
    curl -fsS --max-time 5 --retry 30 --retry-all-errors --retry-delay 1 --retry-max-time 30 \
      http://127.0.0.1:8081/actuator/health/readiness >/dev/null
    ;;
  managed-ids)
    name=${1:?function name required}
    nerdctl --namespace "$namespace" ps -q \
      --filter "label=io.nanofaas.function=$name" \
      --filter 'label=io.nanofaas.backend=containerd'
    ;;
  inspect-owned)
    function_name=${1:?function name required}
    replica=${2:?replica index required}
    python3 - "$CONTAINERD_ADDRESS" "$namespace" "$function_name" "$replica" <<'PY'
import json
import subprocess
import sys

address, namespace, function, replica = sys.argv[1:]
if not replica.isdecimal() or int(replica) < 1:
    raise SystemExit("replica index must be positive")
ctr = ("ctr", "--address", address, "--namespace", namespace, "containers")
identifiers = subprocess.check_output((*ctr, "list", "--quiet"), text=True).splitlines()
matches = []
for identifier in identifiers:
    payload = json.loads(subprocess.check_output((*ctr, "info", identifier), text=True))
    if payload.get("ID", payload.get("id")) != identifier:
        raise SystemExit(f"containerd inspect ID disagrees with inventory: {identifier}")
    labels = payload.get("Labels", payload.get("labels", {}))
    if all((
        labels.get("io.nanofaas.backend") == "containerd",
        labels.get("io.nanofaas.managed") == "true",
        labels.get("io.nanofaas.function") == function,
        labels.get("io.nanofaas.replica") == replica,
    )):
        matches.append(payload)
if len(matches) != 1:
    raise SystemExit(
        f"{function} replica {replica}: expected exactly one owned container "
        f"in {namespace}, found {len(matches)}"
    )
print(json.dumps(matches[0], separators=(",", ":")))
PY
    ;;
  inventory)
    nerdctl --namespace "$namespace" ps -a
    ctr --address "$CONTAINERD_ADDRESS" --namespace "$namespace" tasks list
    ctr --address "$CONTAINERD_ADDRESS" --namespace "$namespace" snapshots list
    rootlessctl --socket "$rk_socket" list-ports
    find "$HOME/.local/share/cni" -type f -print
    ;;
  *) echo "unknown action: $action" >&2; exit 2 ;;
esac
