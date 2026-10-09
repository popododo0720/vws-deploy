#!/usr/bin/env bash
# Install hypervisor agents declared by directories under host/agents/.
# Agent names are directory names. This script does not accept them as arguments.
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)

usage() {
  cat <<'EOF'
Usage: install.sh [--check] [--dry-run] [--control-plane URL] [--bundle DIR] [--prune]

Declare an agent by adding host/agents/<name>/deps.conf.
Do not pass agent names on the command line.

  --check            validate the unit files in this repo and exit
  --dry-run          print the host changes without applying them
  --control-plane    base URL of the control plane (required on first install)
  --bundle DIR       directory of binaries named vws-agent-<name>
  --prune            remove agents installed on this host that are no longer declared
EOF
}

die() {
  echo "$*" >&2
  exit 1
}

dry_run=0
check_only=0
prune=0
control_plane=""
bundle=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --check) check_only=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    --prune) prune=1; shift ;;
    --control-plane)
      [[ $# -ge 2 ]] || { echo "R6: --control-plane needs a URL" >&2; exit 2; }
      control_plane=$2
      shift 2
      ;;
    --bundle)
      [[ $# -ge 2 ]] || { echo "R6: --bundle needs a directory" >&2; exit 2; }
      bundle=$2
      shift 2
      ;;
    --) shift; break ;;
    -*) echo "R6: unknown option $1" >&2; exit 2 ;;
    *) echo "R6: agent names are not arguments; add host/agents/<name>/" >&2; exit 2 ;;
  esac
done
if [[ $# -gt 0 ]]; then
  echo "R6: agent names are not arguments; add host/agents/<name>/" >&2
  exit 2
fi

shopt -s nullglob
agents=()
for dir in "$ROOT/agents"/*/; do
  agents+=("$(basename "$dir")")
done
mapfile -t agents < <(printf '%s\n' "${agents[@]}" | sort)

if [[ ${#agents[@]} -eq 0 ]]; then
  die "R6: no agents declared under host/agents"
fi

validate_deps() {
  local file=$1
  local section="" line key
  [[ -f $file ]] || die "R6: missing $file"
  while IFS= read -r line || [[ -n $line ]]; do
    case "$line" in
      ''|\#*) continue ;;
      \[*\])
        section=${line#[}
        section=${section%]}
        [[ $section == "Unit" ]] || die "R6: $file may only contain a [Unit] section"
        ;;
      *)
        [[ $section == "Unit" ]] || die "R6: $file key is outside [Unit]"
        key=${line%%=*}
        case "$key" in
          After|Before|Wants|Requires|BindsTo|Condition*|Assert*) ;;
          *) die "R6: $file key ${key} is not boot ordering" ;;
        esac
        ;;
    esac
  done < "$file"
}

require_line() {
  local file=$1 needle=$2
  grep -q -F -x "$needle" "$file" || die "R6: $file is missing ${needle}"
}

unit="$ROOT/systemd/vws-agent@.service"
target="$ROOT/systemd/vws-agents.target"
[[ -f $unit && -f $target ]] || die "R6: systemd unit template is missing"

require_line "$unit" "Type=simple"
require_line "$unit" "Restart=always"
require_line "$unit" "RestartSec=2s"
require_line "$unit" "StartLimitIntervalSec=0"
require_line "$unit" "StateDirectory=vws/%i"
require_line "$unit" "ExecStart=/usr/local/bin/vws-agent-%i"
require_line "$unit" "WantedBy=vws-agents.target"
require_line "$unit" "KillMode=process"
require_line "$target" "WantedBy=multi-user.target"

if grep -q -E '^(Type=notify|WatchdogSec=|DynamicUser=|ProtectSystem=)' "$unit"; then
  die "R6: $unit must not require sd_notify or hide the host from the agent"
fi

for name in "${agents[@]}"; do
  validate_deps "$ROOT/agents/$name/deps.conf"
  if [[ -f $ROOT/agents/$name/agent.env ]] && grep -q -E '^\[|^(Restart|ExecStart|User)=' "$ROOT/agents/$name/agent.env"; then
    die "R6: $name/agent.env cannot carry service settings"
  fi
done

if [[ $check_only -eq 1 ]]; then
  exit 0
fi

say() {
  if [[ $dry_run -eq 1 ]]; then
    echo "would $*"
  fi
}

if [[ $dry_run -eq 1 ]]; then
  say "install /etc/systemd/system/vws-agent@.service"
  say "install /etc/systemd/system/vws-agents.target"
  say "enable vws-agents.target"
  for name in "${agents[@]}"; do
    say "install drop-in vws-agent@${name}.service.d/10-deps.conf"
    say "enable vws-agent@${name}.service"
    if [[ ! -x /usr/local/bin/vws-agent-${name} ]]; then
      echo "binary missing: /usr/local/bin/vws-agent-${name} (required for a real install)"
    fi
  done
  exit 0
fi

[[ $(id -u) -eq 0 ]] || die "R6: re-run as root"
command -v systemctl >/dev/null || die "R6: systemctl is required"

env_file=/etc/vws/agent.env
if [[ -n $control_plane ]]; then
  case "$control_plane" in
    http://*|https://*) ;;
    *) die "R6: --control-plane must be an http or https URL" ;;
  esac
elif [[ ! -f $env_file ]]; then
  die "R6: --control-plane is required on first install"
fi

for name in "${agents[@]}"; do
  if [[ -n $bundle ]]; then
    [[ -f $bundle/vws-agent-${name} ]] || die "R6: bundle is missing vws-agent-${name}"
  elif [[ ! -x /usr/local/bin/vws-agent-${name} ]]; then
    die "R6: install /usr/local/bin/vws-agent-${name} or pass --bundle"
  fi
done

install_if_changed() {
  local src=$1 dest=$2 mode=$3
  if [[ -f $dest ]] && cmp -s "$src" "$dest"; then
    return 1
  fi
  mkdir -p "$(dirname "$dest")"
  local tmp
  tmp=$(mktemp "$(dirname "$dest")/.tmp.XXXXXX")
  cp "$src" "$tmp"
  chmod "$mode" "$tmp"
  mv -f "$tmp" "$dest"
  return 0
}

unit_changed=0
if install_if_changed "$unit" /etc/systemd/system/vws-agent@.service 644; then
  unit_changed=1
fi
if install_if_changed "$target" /etc/systemd/system/vws-agents.target 644; then
  unit_changed=1
fi

mkdir -p /etc/vws/agents
if [[ -n $control_plane ]]; then
  tmp=$(mktemp)
  printf 'VWS_CONTROL_PLANE_URL=%s\n' "$control_plane" > "$tmp"
  if install_if_changed "$tmp" "$env_file" 640; then
    unit_changed=1
  fi
  rm -f "$tmp"
fi

declare -A changed=()
for name in "${agents[@]}"; do
  changed[$name]=0
  dropin_dir="/etc/systemd/system/vws-agent@${name}.service.d"
  mkdir -p "$dropin_dir"
  if install_if_changed "$ROOT/agents/$name/deps.conf" "$dropin_dir/10-deps.conf" 644; then
    changed[$name]=1
  fi
  if [[ -n $bundle ]]; then
    if install_if_changed "$bundle/vws-agent-${name}" "/usr/local/bin/vws-agent-${name}" 755; then
      changed[$name]=1
    fi
  fi
  if [[ -f $ROOT/agents/$name/agent.env && ! -f /etc/vws/agents/${name}.env ]]; then
    install -m 640 "$ROOT/agents/$name/agent.env" "/etc/vws/agents/${name}.env"
  fi
done

if [[ $prune -eq 1 ]]; then
  for dropin in /etc/systemd/system/vws-agent@*.service.d; do
    [[ -d $dropin ]] || continue
    base=$(basename "$dropin")
    inst=${base#vws-agent@}
    inst=${inst%.service.d}
    keep=0
    for name in "${agents[@]}"; do
      [[ $name == "$inst" ]] && keep=1
    done
    if [[ $keep -eq 0 ]]; then
      systemctl disable --now "vws-agent@${inst}.service" || true
      rm -rf "$dropin"
      rm -f "/usr/local/bin/vws-agent-${inst}"
    fi
  done
fi

need_reload=$unit_changed
for name in "${agents[@]}"; do
  if [[ ${changed[$name]} -eq 1 ]]; then
    need_reload=1
  fi
done
if [[ $need_reload -eq 1 ]]; then
  systemctl daemon-reload
fi

systemctl enable vws-agents.target
for name in "${agents[@]}"; do
  systemctl enable "vws-agent@${name}.service"
  if [[ $unit_changed -eq 1 || ${changed[$name]} -eq 1 ]]; then
    systemctl restart "vws-agent@${name}.service"
  else
    systemctl start "vws-agent@${name}.service"
  fi
done
