#!/usr/bin/env bash
# Install the pinned rootless containerd topology in a disposable NanoLab VM.
set -euo pipefail

action=${1:?install or check required}
real_home=$(getent passwd "$(id -u)" | cut -d: -f6)
[[ -n $real_home && $real_home == /* ]] || { echo "cannot resolve real home" >&2; exit 2; }
export HOME=$real_home XDG_RUNTIME_DIR=/run/user/$(id -u)
state=$HOME/.local/share/nanolab/containerd-rootless

check() {
  local child controllers
  [[ -S $XDG_RUNTIME_DIR/containerd/containerd.sock ]] || { echo "rootless containerd socket is absent" >&2; exit 1; }
  [[ -r $XDG_RUNTIME_DIR/containerd-rootless/child_pid ]] || { echo "RootlessKit child PID is absent" >&2; exit 1; }
  child=$(cat "$XDG_RUNTIME_DIR/containerd-rootless/child_pid")
  [[ $(stat -c %u "$XDG_RUNTIME_DIR/containerd/containerd.sock") == "$(id -u)" ]] || { echo "rootless socket belongs to another user" >&2; exit 1; }
  for mapping in uid_map gid_map; do
    awk '$3 >= 65536 { found = 1 } END { exit !found }' "/proc/$child/$mapping" || {
      echo "RootlessKit lacks the required 65536 subordinate $mapping entries" >&2; exit 1;
    }
  done
  controllers=$(cat /sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/cgroup.subtree_control)
  for controller in cpu cpuset memory pids; do
    [[ " $controllers " == *" $controller "* ]] || { echo "required cgroup controller $controller is not delegated" >&2; exit 1; }
  done
  nsenter -t "$child" -U --preserve-credentials -n -m true
  [[ -d $HOME/.config/cni/net.d && -d $HOME/.local/share/cni/cache ]] || { echo "rootless CNI directories are absent" >&2; exit 1; }
  crun --version | head -1
  containerd --version
}

wait_for_rootless_socket() {
  local socket=$XDG_RUNTIME_DIR/containerd/containerd.sock attempt
  for attempt in {1..30}; do
    if [[ -S $socket && -r $XDG_RUNTIME_DIR/containerd-rootless/child_pid ]] &&
      timeout 1s ctr --address "$socket" version >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
  done
  echo "rootless containerd did not become ready after restart" >&2
  return 1
}

[[ $action == install ]] || { [[ $action == check ]] && check; exit; }
[[ $(id -u) != 0 ]] || { echo "install as the unprivileged VM user" >&2; exit 2; }
mkdir -p "$state" "$HOME/.config/containerd/certs.d/127.0.0.1:5000" \
  "$HOME/.config/cni/net.d" "$HOME/.local/share/cni/cache" \
  "$HOME/.local/share/cni/ipam" "$HOME/.local/share/nanofaas" \
  "$HOME/.config/systemd/user/containerd.service.d"

sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  crun uidmap dbus-user-session slirp4netns curl openjdk-25-jdk-headless

if ! command -v containerd-rootless-setuptool.sh >/dev/null; then
  arch=$(dpkg --print-architecture)
  case $arch in amd64|arm64) ;; *) echo "unsupported nerdctl architecture: $arch" >&2; exit 2 ;; esac
  archive=/tmp/nanolab-nerdctl-full-2.3.5-$arch.tgz
  curl -fsSL -o "$archive" \
    "https://github.com/containerd/nerdctl/releases/download/v2.3.5/nerdctl-full-2.3.5-linux-$arch.tar.gz"
  sudo tar Cxzf /usr/local "$archive"
fi

sudo tee /etc/apparmor.d/usr.local.bin.rootlesskit >/dev/null <<'PROFILE'
abi <abi/4.0>,
include <tunables/global>
/usr/local/bin/rootlesskit flags=(unconfined) {
  userns,
  include if exists <local/usr.local.bin.rootlesskit>
}
PROFILE
sudo systemctl restart apparmor.service
sudo loginctl enable-linger "$(id -un)"

cat > "$HOME/.config/systemd/user/containerd.service.d/10-nanolab-topology.conf" <<'UNIT'
[Service]
Environment=CONTAINERD_ROOTLESS_ROOTLESSKIT_NET=slirp4netns
Environment=CONTAINERD_ROOTLESS_ROOTLESSKIT_DETACH_NETNS=false
UNIT

sudo install -d /etc/systemd/system/user@.service.d
dropin=/etc/systemd/system/user@.service.d/10-nanolab-delegate.conf
if [[ ! -f $dropin ]] || ! grep -qx 'Delegate=cpu cpuset memory pids' "$dropin"; then
  printf '[Service]\nDelegate=cpu cpuset memory pids\n' | sudo tee "$dropin" >/dev/null
  touch "$state/needs-reboot"
fi
sudo systemctl daemon-reload

if [[ ! -f $HOME/.config/systemd/user/containerd.service ]]; then
  containerd-rootless-setuptool.sh install
fi
systemctl --user daemon-reload

cat > "$HOME/.config/cni/net.d/10-nanofaas.conflist" <<CONF
{
  "cniVersion": "1.0.0",
  "name": "nanofaas",
  "plugins": [
    {"type": "bridge", "bridge": "nanofaas0", "isGateway": true,
     "ipMasq": true,
     "ipam": {"type": "host-local", "dataDir": "$HOME/.local/share/cni/ipam",
              "ranges": [[{"subnet": "10.90.0.0/24"}]],
              "routes": [{"dst": "0.0.0.0/0"}]},
     "dns": {"nameservers": ["10.0.2.3", "1.1.1.1"]}},
    {"type": "firewall"},
    {"type": "loopback"}
  ]
}
CONF

cat > "$HOME/.config/containerd/certs.d/127.0.0.1:5000/hosts.toml" <<'HOSTS'
server = "http://127.0.0.1:5000"
[host."http://127.0.0.1:5000"]
  capabilities = ["pull", "resolve", "push"]
HOSTS

python3 - "$HOME/.config/containerd/config.toml" "$HOME/.config/containerd/certs.d" <<'PY'
from pathlib import Path
import sys
import platform

path, hosts = Path(sys.argv[1]), sys.argv[2]
config = path.read_text() if path.exists() else 'version = 3\n'
section = '[plugins."io.containerd.transfer.v1.local"]'
if 'version = 3' not in config:
    raise SystemExit('rootless containerd config must use version 3')
if section + '\n' not in config:
    config = config.replace('version = 3\n', f'version = 3\n{section}\n', 1)
if f'config_path = "{hosts}"' not in config:
    config = config.replace(section + '\n', section + f'\n  config_path = "{hosts}"\n', 1)
if 'unpack_config' not in config:
    arch = {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(platform.machine())
    if arch is None:
        raise SystemExit(f'unsupported containerd architecture: {platform.machine()}')
    config += '\n[[plugins."io.containerd.transfer.v1.local".unpack_config]]\n'
    config += f'  platform = "linux/{arch}"\n  snapshotter = "native"\n  differ = "walking"\n'
path.write_text(config)
PY

systemctl --user restart containerd.service
if [[ -f $state/needs-reboot ]]; then
  echo "delegation changed; VM reboot required before rootless check"
else
  wait_for_rootless_socket
  check
fi
