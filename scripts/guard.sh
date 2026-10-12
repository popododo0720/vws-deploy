#!/usr/bin/env bash
# Repo checks that helm lint and kubeconform cannot see.
set -euo pipefail
export LC_ALL=C

ROOT=$(CDPATH= cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

die() {
  echo "$*" >&2
  exit 1
}

scope_owns_file() {
  local file=$1 scope=$2 path=$3 pref matched=0
  while read -r line; do
    [[ -z $line || $line =~ ^# ]] && continue
    # shellcheck disable=SC2086
    set -- $line
    [[ $1 == "$scope" ]] || continue
    shift
    for pref in "$@"; do
      if [[ $pref == */ ]]; then
        [[ $path == "$pref"* ]] && matched=1
      else
        [[ $path == "$pref" ]] && matched=1
      fi
    done
  done < "$file"
  [[ $matched -eq 1 ]]
}

scope_owner_file() {
  local file=$1 path=$2 scope owners=0 found=""
  while read -r line; do
    [[ -z $line || $line =~ ^# ]] && continue
    scope=${line%% *}
    if scope_owns_file "$file" "$scope" "$path"; then
      owners=$((owners + 1))
      found=$scope
    fi
  done < "$file"
  [[ $owners -eq 1 ]] || return 1
  printf '%s\n' "$found"
}

scope_regex() {
  awk 'NF && $1 !~ /^#/ { print $1 }' scripts/scopes | paste -sd'|' -
}

cmd_layout() {
  local found images
  found=$(find charts/vws/templates -type f -printf '%f\n' | sort | paste -sd' ' -)
  [[ $found == "_helpers.tpl control-plane.yaml" ]] || die "R10: unexpected template files: ${found}"
  [[ ! -d charts/vws/charts ]] || die "R10: subcharts are a second catalog"
  if grep -q -E '^(dependencies|icon):' charts/vws/Chart.yaml; then
    die "R10: Chart.yaml must not declare dependencies or an icon"
  fi
  if find . -path ./.git -prune -o \( -name '*.service' -o -name '*.target' \) -print | grep -q .; then
    die "R10: this repo ships no systemd units"
  fi
  [[ ! -e host ]] || die "R10: host/ must not exist"
  if grep -R -n -E 'DaemonSet|hostPath:|hostNetwork:' charts; then
    die "R10: chart templates must not schedule onto the host"
  fi
  images=0
  if image_lines=$(grep -R -o 'printf "%s/%s:%s"' charts/vws/templates); then
    images=$(printf '%s\n' "$image_lines" | wc -l)
  fi
  [[ $images -eq 1 ]] || die "R5: derive the image reference in exactly one place"
  if [[ -f go.mod || -f kustomization.yaml ]]; then
    die "R10: the Helm chart is the only manifest path"
  fi
}

cmd_names() {
  python3 - <<'PY'
import re
import sys
from pathlib import Path

import yaml

values = yaml.safe_load(Path("charts/vws/values.yaml").read_text())
text = "\n".join(
    path.read_text()
    for path in Path("charts/vws/templates").rglob("*")
    if path.is_file()
)
failed = False
for name in values["services"]:
    literal = re.escape(name)
    hardcoded = re.search(rf"(?m)^[^#\n]*name:\s*{literal}\s*$", text)
    compared = re.search(rf"""eq\s+\S+\s+["']{literal}["']""", text)
    if hardcoded or compared:
        print(f"R5: template hardcodes service name {name}", file=sys.stderr)
        failed = True
if failed:
    sys.exit(1)
PY
}

cmd_assets() {
  python3 - <<'PY'
import sys
from pathlib import Path

skip = {".git", "build", ".cache"}
images = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp"}
needles = [
    "all rights " + "reserved",
    "not " + "affiliated",
    "trade" + "mark of",
    "ama" + "zon",
    chr(0x2122),
    chr(0x00AE),
]
failed = False
for path in Path(".").rglob("*"):
    if not path.is_file():
        continue
    if any(part in skip for part in path.parts):
        continue
    if path.suffix.lower() in images:
        print(f"R7: image file {path}", file=sys.stderr)
        failed = True
        continue
    if path.name == "LICENSE":
        continue
    text = path.read_text(errors="ignore").lower()
    for needle in needles:
        if needle.lower() in text:
            print(f"R7: brand-ownership notice in {path}", file=sys.stderr)
            failed = True
            break
if failed:
    sys.exit(1)
PY
}

cmd_ci() {
  if grep -R -n -E 'kubectl[[:space:]]+(apply|create|delete|replace)|helm[[:space:]]+(install|upgrade|uninstall)|kind[[:space:]]+create|k3d[[:space:]]+create' \
    .github Makefile scripts charts --exclude-dir=lab; then
    die "R8: this repo must not apply manifests to a cluster"
  fi
  grep -q 'make check' .github/workflows/ci.yml || die "R8: CI must run make check"
  grep -q 'helm lint' Makefile || die "R8: make check must run helm lint"
  grep -q 'kubeconform' Makefile || die "R8: make check must run kubeconform"
}

cmd_schema() {
  python3 - <<'PY'
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

values = yaml.safe_load(Path("charts/vws/values.yaml").read_text())
services = values["services"]
stateless = next((name for name, spec in services.items() if "state" not in (spec or {})), None)
stateful = next((name for name, spec in services.items() if "state" in (spec or {})), None)
wildcard_rule = {"apiGroups": ["kubevirt.io"], "resources": ["virtualmachines"], "verbs": ["get"]}
cases = [
    {"services": {"compute": {"rbac": [{**wildcard_rule, "resources": ["*"]}]}}},
    {"services": {"compute": {"rbac": [{**wildcard_rule, "apiGroups": ["*"]}]}}},
    {"services": {"compute": {"rbac": [{**wildcard_rule, "verbs": ["*"]}]}}},
    {"services": {"compute": {"rbac": [{**wildcard_rule, "verbs": ["escalate"]}]}}},
    {"services": {"compute": {"rbac": [{**wildcard_rule, "namespace": "Not_A_Namespace"}]}}},
    {"services": {"compute": {"rbac": []}}},
    {"services": {"compute-agent": {}}},
    {"services": {"compute-peers": {}}},
]
if stateless is not None:
    cases.append({"services": {stateless: {"replicas": 1}}})
    cases.append({"services": {stateless: {"livenessProbe": {}}}})
if stateful is not None:
    cases.append({"services": {stateful: {"replicas": 3}}})
for case in cases:
    handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    yaml.safe_dump(case, handle)
    handle.close()
    proc = subprocess.run(
        ["helm", "lint", "charts/vws", "--strict", "-f", handle.name],
        capture_output=True,
        text=True,
    )
    os.unlink(handle.name)
    blob = proc.stdout + proc.stderr
    if proc.returncode == 0:
        print(f"R5: schema accepted {case}", file=sys.stderr)
        sys.exit(1)
    if "values don't meet the specifications" not in blob and "Additional property" not in blob:
        print(blob, file=sys.stderr)
        print("R5: helm lint failed for a reason other than the values schema", file=sys.stderr)
        sys.exit(1)
PY
}

cmd_hooks() {
  local rc=0
  set +e
  printf '%s\n' 'refs/heads/topic 111 refs/heads/main 222' | .githooks/pre-push >/dev/null 2>&1
  rc=$?
  set -e
  [[ $rc -ne 0 ]] || die "R1: pre-push accepted a push to main"
  set +e
  printf '%s\n' 'refs/heads/topic 111 refs/heads/master 222' | .githooks/pre-push >/dev/null 2>&1
  rc=$?
  set -e
  [[ $rc -ne 0 ]] || die "R1: pre-push accepted a push to master"
  printf '%s\n' 'refs/heads/topic 111 refs/heads/topic 222' | .githooks/pre-push >/dev/null
  local tmp
  tmp=$(mktemp)
  printf '%s\n' 'not a scoped subject' > "$tmp"
  set +e
  scripts/guard.sh commit-msg "$tmp" >/dev/null 2>&1
  rc=$?
  set -e
  if [[ $rc -eq 0 ]]; then
    rm -f "$tmp"
    die "R2: commit-msg accepted an unscoped subject"
  fi
  printf '%s\n' 'chart: add one service key' > "$tmp"
  scripts/guard.sh commit-msg "$tmp" >/dev/null
  rm -f "$tmp"
}

cmd_docs() {
  python3 - <<'PY'
import re
import sys
from pathlib import Path

text = Path("AGENTS.md").read_text()
for heading in ("## 규칙", "## 브랜치와 원자적 커밋", "## pstack 사용", "## 명령"):
    if heading not in text:
        print(f"R2: AGENTS.md is missing {heading}", file=sys.stderr)
        sys.exit(1)
for word in ("architect", "interrogate", "correct"):
    if word not in text:
        print(f"R2: pstack usage is missing {word}", file=sys.stderr)
        sys.exit(1)

rows = re.findall(r"^\| (R\d+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \|$", text, re.M)
if not rows:
    print("R2: AGENTS.md has no rule table", file=sys.stderr)
    sys.exit(1)
ids = [row[0] for row in rows]
if len(ids) != len(set(ids)):
    print("R2: duplicate rule ids", file=sys.stderr)
    sys.exit(1)

emitted = set()
for path in (Path("scripts/guard.sh"), Path("scripts/check_render.py"), Path(".githooks/pre-push")):
    emitted.update(re.findall(r"\bR\d+\b", path.read_text()))
if set(ids) != emitted:
    print(f"R2: rule ids {sorted(set(ids))} != checks {sorted(emitted)}", file=sys.stderr)
    sys.exit(1)

for rule_id, _rule, structure, check in rows:
    structure = structure.strip()
    check = check.strip()
    if structure == "-" and check in {"", "-"}:
        print(f"R2: {rule_id} has no structure and no check", file=sys.stderr)
        sys.exit(1)
    if structure != "-":
        paths = re.findall(r"`([^`]+)`", structure)
        if not paths:
            print(f"R2: {rule_id} structure does not point at a path", file=sys.stderr)
            sys.exit(1)
        for rel in paths:
            if not Path(rel).exists():
                print(f"R2: {rule_id} structure path {rel} does not exist", file=sys.stderr)
                sys.exit(1)
    commands = re.findall(r"`([^`]+)`", check)
    if not commands:
        print(f"R2: {rule_id} check does not point at a command", file=sys.stderr)
        sys.exit(1)
    for command in commands:
        program = command.split()[0]
        if program == "make":
            target = command.split()[1]
            makefile = Path("Makefile").read_text()
            if not re.search(rf"^{re.escape(target)}:", makefile, re.M):
                print(f"R2: {rule_id} Makefile has no target {target}", file=sys.stderr)
                sys.exit(1)
        elif not Path(program).exists():
            print(f"R2: {rule_id} check program {program} does not exist", file=sys.stderr)
            sys.exit(1)
PY
}

cmd_commit_msg() {
  local file=$1 subject scopes_re
  [[ -n $file && -f $file ]] || die "R2: commit-msg needs the message file"
  subject=$(head -n 1 "$file")
  scopes_re=$(scope_regex)
  if [[ ! $subject =~ ^(${scopes_re})(\([a-z0-9-]+\))?:\ .+ ]]; then
    die "R2: subject must be '<scope>: summary' using scripts/scopes (got: ${subject})"
  fi
  case "$subject" in
    fixup!*|squash!*|WIP*|wip*) die "R2: subject is not a finished commit" ;;
  esac
}

scopes_snapshot() {
  local sha=$1 dest=$2 parent
  parent=$(git rev-parse "${sha}^")
  if git cat-file -e "${parent}:scripts/scopes" 2>/dev/null; then
    git show "${parent}:scripts/scopes" > "$dest"
  elif git cat-file -e "${sha}:scripts/scopes" 2>/dev/null; then
    git show "${sha}:scripts/scopes" > "$dest"
  else
    cp scripts/scopes "$dest"
  fi
}

cmd_commits() {
  local base=${1:-origin/main}
  if ! git rev-parse --verify "$base" >/dev/null 2>&1; then
    die "R2: base ${base} is not available"
  fi
  local sha subject scope parents path owner snapshot
  local -a shas files
  snapshot=$(mktemp)
  mapfile -t shas < <(git rev-list --reverse "${base}..HEAD")
  for sha in "${shas[@]}"; do
    [[ -n $sha ]] || continue
    parents=$(git rev-list --parents -n 1 "$sha")
    # shellcheck disable=SC2086
    set -- $parents
    [[ $# -le 2 ]] || die "R2: ${sha} is a merge commit"
    subject=$(git log -1 --format=%s "$sha")
    scopes_snapshot "$sha" "$snapshot"
    local scopes_re
    scopes_re=$(awk 'NF && $1 !~ /^#/ { print $1 }' "$snapshot" | paste -sd'|' -)
    if [[ ! $subject =~ ^(${scopes_re})(\([a-z0-9-]+\))?:\ .+ ]]; then
      rm -f "$snapshot"
      die "R2: ${sha} subject is not '<scope>: summary' (${subject})"
    fi
    scope=${BASH_REMATCH[1]}
    mapfile -t files < <(git diff-tree --no-commit-id --name-only -r "$sha")
    [[ ${#files[@]} -gt 0 ]] || die "R2: ${sha} changes no files"
    for path in "${files[@]}"; do
      if ! owner=$(scope_owner_file "$snapshot" "$path"); then
        rm -f "$snapshot"
        die "R2: ${path} must belong to exactly one scope"
      fi
      if [[ $owner != "$scope" ]]; then
        rm -f "$snapshot"
        die "R2: ${sha} (${scope}) touches ${path}, owned by ${owner}; split the commit"
      fi
    done
  done
  rm -f "$snapshot"
}

cmd_recovery() {
  local rendered
  rendered=$(mktemp)
  local release=${VWS_RELEASE:-vws}
  helm template "$release" charts/vws --namespace "$release" > "$rendered"
  python3 scripts/check_render.py "$rendered" charts/vws/values.yaml "$release"
  rm -f "$rendered"
}

cmd_cursor() {
  [[ ! -e .cursor ]] || die "R9: .cursor/ does not belong in this repo"
  if grep -R -n -i -E 'api\.cursor\.com|cursor-agent|cursor cloud|CloudAgent' .github Makefile scripts charts 2>/dev/null \
    | grep -v 'scripts/guard.sh:'; then
    die "R9: Cursor cloud agents must not be called from this repo"
  fi
  grep -q 'Cursor 클라우드 에이전트' AGENTS.md || die "R9: AGENTS.md lost the Cursor rule"
}

cmd_bundle() {
  local name version hits
  for name in KUBEVIRT CDI MULTUS KUBEOVN ROOK K3S; do
    version=$(sed -n "s/^${name}_VERSION=v\{0,1\}//p" scripts/versions.env)
    [[ -n $version ]] || die "R12: scripts/versions.env has no ${name}_VERSION"
    hits=$(grep -R -n -F -e "$version" . --exclude-dir=.git --exclude-dir=build --exclude-dir=.cache \
      --exclude=versions.env || true)
    [[ -z $hits ]] || die "R12: ${name} version is written outside scripts/versions.env: ${hits}"
  done
  local k8s k3s
  k8s=$(sed -n 's/^KUBERNETES_VERSION=\([0-9]*\.[0-9]*\)\..*/\1/p' scripts/versions.env)
  k3s=$(sed -n 's/^K3S_VERSION=v\([0-9]*\.[0-9]*\)\..*/\1/p' scripts/versions.env)
  [[ -n $k8s && -n $k3s ]] || die "R12: scripts/versions.env needs KUBERNETES_VERSION and K3S_VERSION as major.minor.patch"
  [[ $k8s == "$k3s" ]] || die "R12: KUBERNETES_VERSION is ${k8s} but K3S_VERSION is ${k3s}"
  cmd_cdi_cr
  for name in rbd openvswitch geneve kvm; do
    grep -q -w "$name" docs/platform-bundle.md || die "R12: docs/platform-bundle.md does not list kernel module ${name}"
  done
  cmd_storage_names
}

cmd_storage_names() {
  local rendered=build/platform/31-rook-ceph-cluster.yaml
  [[ -f $rendered ]] || die "R12: ${rendered} is missing; run scripts/platform.sh first"
  python3 - "$rendered" <<'PY'
import sys
from pathlib import Path

import yaml

WANT = "ceph-block"
docs = [d for d in yaml.safe_load_all(Path(sys.argv[1]).read_text()) if d]
classes = [d for d in docs if d["kind"] == "StorageClass" and d["metadata"]["name"] == WANT]
snaps = [d for d in docs if d["kind"] == "VolumeSnapshotClass" and d["metadata"]["name"] == WANT]
if len(classes) != 1 or len(snaps) != 1:
    sys.exit(f"R12: the bundle must render StorageClass and VolumeSnapshotClass {WANT} once each")
if classes[0]["provisioner"] != snaps[0]["driver"]:
    sys.exit("R12: StorageClass provisioner and VolumeSnapshotClass driver differ")
doc = Path("docs/platform-bundle.md").read_text()
for kind in ("StorageClass", "VolumeSnapshotClass"):
    if not any(line.startswith(f"| {kind} | `{WANT}` |") for line in doc.splitlines()):
        sys.exit(f"R12: docs/platform-bundle.md must list {kind} {WANT}")
PY
}

cmd_cdi_cr() {
  local crd=.cache/platform/cdi-operator.yaml
  [[ -f $crd ]] || die "R12: ${crd} is missing; run scripts/platform.sh first"
  python3 - "$crd" <<'PY'
import sys
from pathlib import Path

import yaml

docs = [d for d in yaml.safe_load_all(Path(sys.argv[1]).read_text()) if d]
crd = next(d for d in docs if d["kind"] == "CustomResourceDefinition" and d["metadata"]["name"] == "cdis.cdi.kubevirt.io")
cr = yaml.safe_load(Path("platform/cdi-cr.yaml").read_text())
version = cr["apiVersion"].split("/")[1]
schema = next(v for v in crd["spec"]["versions"] if v["name"] == version)["schema"]["openAPIV3Schema"]


def walk(value, node, path):
    if "properties" in node and isinstance(value, dict):
        for key, item in value.items():
            if key not in node["properties"]:
                sys.exit(f"R12: platform/cdi-cr.yaml {path}.{key} is not in the CDI CRD schema")
            walk(item, node["properties"][key], f"{path}.{key}")
    elif node.get("type") == "array" and isinstance(value, list):
        for item in value:
            walk(item, node.get("items", {}), f"{path}[]")
    elif node.get("type") == "string" and not isinstance(value, str):
        sys.exit(f"R12: platform/cdi-cr.yaml {path} must be a string")


walk(cr["spec"], schema["properties"]["spec"], "spec")
PY
}

cmd_agents() {
  scripts/guard-agents.sh agents AGENTS.md || die "R13: AGENTS.md lost the Insider-risk guardrails"
  scripts/guard-agents.sh selftest >/dev/null || die "R13: scripts/guard-agents.sh accepted a bad input"
  grep -q 'guard-agents.sh pr-base' .github/workflows/repo-guard.yml || die "R13: repo-guard.yml does not check the base"
  grep -q 'guard-agents.sh pr-body' .github/workflows/repo-guard.yml || die "R13: repo-guard.yml does not check Closes #N"
  grep -q 'guard-agents.sh push-watch' .github/workflows/direct-push-watch.yml || die "R13: direct-push-watch.yml does not watch pushes to main"
  ! grep -q 'push:' .github/workflows/repo-guard.yml || die "R13: repo-guard.yml must run on pull requests only, so a pull request has no skipped check"
}

cmd_config() {
  local tmp literal
  tmp=$(mktemp -d)
  while read -r literal; do
    [[ -n $literal ]] || continue
    if grep -R -n -F -e "$literal" charts/vws/templates; then
      rm -rf "$tmp"
      die "R14: namespace ${literal} is written in templates; names live only in values namespaces"
    fi
  done < <(python3 -c 'import yaml;print(*yaml.safe_load(open("charts/vws/values.yaml"))["namespaces"].values(), sep="\n")')

  helm template vws charts/vws --namespace vws > "$tmp/default.yaml"
  grep -q -e '- -kube-namespace=vws-vms' "$tmp/default.yaml" || die "R14: storage does not get -kube-namespace from values"
  ! grep -q -e '-vm-namespace' "$tmp/default.yaml" || die "R14: pendingFlags are rendered while global.pendingFlags is false"

  cat > "$tmp/override.yaml" <<'YAML'
global:
  pendingFlags: true
namespaces:
  vms: acme-vms
  images: acme-images
  nad: acme-nad
services:
  network:
    flags:
      kubeconfig: /etc/vws/kubeconfig
      kube-external-subnet: ext
      kube-nad-namespace: {namespace: nad}
      kube-snat: true
YAML
  helm template acme charts/vws --namespace acme -f "$tmp/override.yaml" > "$tmp/override.rendered.yaml"
  python3 scripts/check_render.py "$tmp/override.rendered.yaml" charts/vws/values.yaml acme "$tmp/override.yaml"
  local want
  for want in -vm-namespace=acme-vms -image-namespace=acme-images -kube-namespace=acme-vms \
    -kube-nad-namespace=acme-nad -kube-external-subnet=ext -kube-snat=true -namespace=acme -hypervisor=kubevirt; do
    grep -q -e "- ${want}\$" "$tmp/override.rendered.yaml" || die "R14: rendered args lack ${want}"
  done
  ! grep -q -e 'vws-vms\|vws-images\|vws-nad' "$tmp/override.rendered.yaml" || die "R14: a default namespace name survived a values override"

  local bad
  for bad in \
    'services: {compute: {rbac: [{namespace: nowhere, apiGroups: [""], resources: [configmaps], verbs: [get]}]}}' \
    'services: {storage: {flags: {kube-namespace: {namespace: nowhere}}}}' \
    'services: {network: {flags: {kubeconfig: /k}}}' \
    'services: {network: {flags: {kubeconfig: /k, kube-external-subnet: ext}}}' \
    'services: {network: {flags: {kube-snat: true}}}' \
    'services: {network: {flags: {kube-x: [a]}}}'; do
    printf '%s\n' "$bad" > "$tmp/bad.yaml"
    if helm template vws charts/vws --namespace vws -f "$tmp/bad.yaml" >/dev/null 2>&1; then
      rm -rf "$tmp"
      die "R14: chart accepted ${bad}"
    fi
  done
  rm -rf "$tmp"
}

cmd_all() {
  cmd_layout
  cmd_names
  cmd_assets
  cmd_ci
  cmd_cursor
  cmd_agents
  cmd_config
  cmd_bundle
  cmd_schema
  cmd_hooks
  cmd_docs
  cmd_recovery
  if [[ -n ${VWS_BASE_REF:-} ]]; then
    cmd_commits "origin/${VWS_BASE_REF}"
  elif git rev-parse --verify origin/main >/dev/null 2>&1; then
    cmd_commits origin/main
  fi
}

usage() {
  cat <<'EOF' >&2
usage: guard.sh <layout|names|assets|ci|cursor|agents|bundle|config|schema|hooks|docs|recovery|commits|commit-msg|all>
EOF
}

command_name=${1:-}
shift || true
case "$command_name" in
  layout) cmd_layout ;;
  names) cmd_names ;;
  assets) cmd_assets ;;
  ci) cmd_ci ;;
  cursor) cmd_cursor ;;
  agents) cmd_agents ;;
  bundle) cmd_bundle ;;
  config) cmd_config ;;
  schema) cmd_schema ;;
  hooks) cmd_hooks ;;
  docs) cmd_docs ;;
  recovery) cmd_recovery ;;
  commits) cmd_commits "$@" ;;
  commit-msg) cmd_commit_msg "$@" ;;
  all) cmd_all ;;
  *) usage; exit 2 ;;
esac
