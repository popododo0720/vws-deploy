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

# r8_scan PATH...: print lines that change a cluster. The only allowed callers are exactly
# vws_kubectl and vws_helm (the vws CLI wrappers with --dry-run); any other prefix fails.
r8_scan() {
  grep -R -n -P '(?<![\w-])(?!vws_kubectl\s|vws_helm\s)[\w-]*(?:kubectl\s+(?:apply|create|delete|replace)|helm\s+(?:install|upgrade|uninstall))\b|(?<![\w-])(?:kind|k3d)\s+create\b' \
    --exclude-dir=lab "$@"
}

cmd_ci() {
  if r8_scan .github Makefile scripts charts; then
    die "R8: this repo must not apply manifests to a cluster"
  fi
  local f
  for f in tests/fixtures/r8/bad/*; do
    r8_scan "$f" >/dev/null || die "R8: negative test ${f} was accepted"
  done
  for f in tests/fixtures/r8/good/*; do
    ! r8_scan "$f" >/dev/null || die "R8: positive test ${f} was rejected"
  done
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

  ! grep -q -e 'httpGet' "$tmp/default.yaml" || die "R15: a service probe is HTTP; the services have no /healthz"
  grep -q -e 'tcpSocket' "$tmp/default.yaml" || die "R15: default probes are not tcpSocket"
  helm template vws charts/vws --namespace vws --set global.probe=grpc > "$tmp/grpc.yaml"
  python3 scripts/check_render.py "$tmp/grpc.yaml" charts/vws/values.yaml vws <(printf 'global:\n  probe: grpc\n')
  grep -q -e 'grpc:' "$tmp/grpc.yaml" || die "R15: global.probe=grpc does not render gRPC probes"
  ! grep -q -e 'tcpSocket' "$tmp/grpc.yaml" || die "R15: global.probe=grpc left a tcpSocket probe"
  ! grep -q -e 'service: vws.storage.v1.Ready' "$tmp/default.yaml" || die "R15: storage gRPC readiness is rendered while global.pendingFlags is false"
  helm template vws charts/vws --namespace vws --set global.pendingFlags=true > "$tmp/health.yaml"
  grep -q -e 'service: vws.storage.v1.Ready' "$tmp/health.yaml" || die "R15: global.pendingFlags=true does not render storage gRPC readiness"
  python3 scripts/check_render.py "$tmp/health.yaml" charts/vws/values.yaml vws <(printf 'global:\n  pendingFlags: true\n')

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
      kubeconfig: /etc/vws/kube/config
      kube-external-subnet: ext
      kube-nad-namespace: {namespace: nad}
      kube-snat: true
    kubeconfigSecret: acme-network-kubeconfig
  storage:
    s3:
      rgw: {enabled: true, endpoint: "http://rgw.acme:8080", bucketPrefix: acme, existingSecret: acme-rgw-s3}
YAML
  helm template acme charts/vws --namespace acme -f "$tmp/override.yaml" > "$tmp/override.rendered.yaml"
  python3 scripts/check_render.py "$tmp/override.rendered.yaml" charts/vws/values.yaml acme "$tmp/override.yaml"
  local want
  for want in -vm-namespace=acme-vms -image-namespace=acme-images -kube-namespace=acme-vms \
    -kube-nad-namespace=acme-nad -kube-external-subnet=ext -kube-snat=true -kubeconfig=/etc/vws/kube/config -namespace=acme -hypervisor=kubevirt \
    -object-backend=s3 -s3-profile=rgw -s3-endpoint=http://rgw.acme:8080 -s3-bucket-prefix=acme; do
    grep -q -e "- ${want}\$" "$tmp/override.rendered.yaml" || die "R14: rendered args lack ${want}"
  done
  ! grep -q -e 'vws-vms\|vws-images\|vws-nad' "$tmp/override.rendered.yaml" || die "R14: a default namespace name survived a values override"

  grep -q -e 'secretName: acme-network-kubeconfig' "$tmp/override.rendered.yaml" || die "R16: kubeconfigSecret is not mounted"
  # -s3-bucket-prefix is not gated by global.pendingFlags since vws-storage#46 (#105).
  helm template vws charts/vws --namespace vws --set services.storage.s3.rgw.enabled=true \
    --set services.storage.s3.rgw.endpoint=http://r:8080 --set services.storage.s3.rgw.bucketPrefix=lab |
    grep -q -e '- -s3-bucket-prefix=lab$' || die "R14: bucketPrefix does not render without global.pendingFlags"
  ! grep -q -e '^kind: Secret' "$tmp/default.yaml" "$tmp/override.rendered.yaml" || die "R16: the chart renders a Secret"
  ! grep -q -e 'kubeconfig' "$tmp/default.yaml" || die "R16: the default render mounts or passes a kubeconfig"
  grep -q -e 'name: acme-rgw-s3' "$tmp/override.rendered.yaml" || die "R16: s3.rgw existingSecret is not referenced"
  ! grep -q -e 'VWS_S3_\|object-backend' "$tmp/default.yaml" || die "R16: the default render enables the s3 object backend"
  # bucketPrefix: at most 18 characters, so <prefix>-vws-<40 hex> fits 63 (vws-storage#46, #103).
  printf '%s\n' 'global: {pendingFlags: true}' 'services: {storage: {s3: {rgw: {enabled: true, endpoint: "http://r:8080", bucketPrefix: abcdefghij12345678}}}}' >"$tmp/prefix.yaml"
  helm template vws charts/vws --namespace vws -f "$tmp/prefix.yaml" | grep -q -e '- -s3-bucket-prefix=abcdefghij12345678$' ||
    die "R16: an 18-character bucketPrefix does not render"

  local bad
  for bad in \
    'services: {compute: {rbac: [{namespace: nowhere, apiGroups: [""], resources: [configmaps], verbs: [get]}]}}' \
    'services: {storage: {flags: {kube-namespace: {namespace: nowhere}}}}' \
    'services: {network: {flags: {kubeconfig: /k}}}' \
    'services: {network: {flags: {kubeconfig: /k, kube-external-subnet: ext}}}' \
    'services: {network: {flags: {kube-snat: true}}}' \
    'services: {network: {kubeconfigSecret: x}}' \
    'services: {network: {kubeconfigSecret: Bad_Name, flags: {kubeconfig: /k/c, kube-external-subnet: e, kube-nad-namespace: {namespace: nad}}}}' \
    'services: {network: {flags: {kubeconfig: /k/c, kube-external-subnet: e, kube-nad-namespace: {namespace: nad}}}}' \
    'services: {network: {kubeconfigSecret: x, flags: {kubeconfig: rel, kube-external-subnet: e, kube-nad-namespace: {namespace: nad}}}}' \
    'services: {network: {flags: {kube-x: [a]}}}' \
    'services: {storage: {s3: {rgw: {enabled: true}}}}' \
    'services: {storage: {s3: {rgw: {enabled: true, endpoint: "http://r:8080", existingSecret: ""}}}}' \
    'services: {storage: {s3: {rgw: {enabled: true, endpoint: "http://r:8080", accessKey: x}}}}' \
    'services: {storage: {s3: {rgw: {enabled: true, endpoint: "http://r:8080", bucketPrefix: abcdefghij123456789}}}}' \
    'services: {storage: {s3: {rgw: {enabled: true, endpoint: "http://r:8080", bucketPrefix: Acme}}}}' \
    'services: {storage: {s3: {rgw: {enabled: true, endpoint: "http://r:8080", bucketPrefix: -acme}}}}' \
    'global: {probe: http}'; do
    printf '%s\n' "$bad" > "$tmp/bad.yaml"
    if helm template vws charts/vws --namespace vws -f "$tmp/bad.yaml" >/dev/null 2>&1; then
      rm -rf "$tmp"
      die "R14: chart accepted ${bad}"
    fi
  done
  rm -rf "$tmp"
}

# vws_layout_check DIR: every scripts/vws.d/<name>.sh has a summary, vws_cmd_main, a test and a doc
# entry, and no command calls kubectl or helm directly (only vws_kubectl and vws_helm honor --dry-run).
vws_layout_check() {
  local root=$1 file name
  [[ -x ${root}/scripts/vws ]] || { echo "R17: scripts/vws is not executable" >&2; return 1; }
  [[ -x ${root}/tests/vws/run.sh ]] || { echo "R17: tests/vws/run.sh is not executable" >&2; return 1; }
  for file in "${root}"/scripts/vws.d/*.sh; do
    [[ -f $file ]] || continue
    name=$(basename "$file" .sh)
    grep -q '^# summary: ' "$file" || { echo "R17: vws.d/${name}.sh has no '# summary:' line" >&2; return 1; }
    grep -q '^vws_cmd_main() {' "$file" || { echo "R17: vws.d/${name}.sh defines no vws_cmd_main" >&2; return 1; }
    [[ -f ${root}/tests/vws/${name}.test.sh ]] || { echo "R17: vws.d/${name}.sh has no tests/vws/${name}.test.sh" >&2; return 1; }
    grep -q -x "### vws ${name}" "${root}/docs/vws-cli.md" || { echo "R17: docs/vws-cli.md has no '### vws ${name}'" >&2; return 1; }
    if grep -v '^[[:space:]]*#' "$file" | grep -q -E '(^|[;&|(])[[:space:]]*(kubectl|helm)[[:space:]]+[a-z-]'; then
      echo "R17: vws.d/${name}.sh calls kubectl or helm directly; use vws_kubectl and vws_helm" >&2
      return 1
    fi
  done
}

cmd_vws() {
  local tmp
  vws_layout_check . || die "R17: scripts/vws layout is broken"
  bash tests/vws/run.sh >/dev/null || die "R17: the vws harness fails; run tests/vws/run.sh"
  if command -v shellcheck >/dev/null; then
    shellcheck -x -S warning scripts/vws scripts/vws.d/*.sh scripts/vws.d/lib/*.sh tests/vws/*.sh tests/fixtures/bin/* tests/fixtures/commands/*.sh ||
      die "R17: shellcheck rejects the vws files"
  fi
  # The layout check must fail on each broken copy.
  tmp=$(mktemp -d)
  cp -r scripts tests docs "$tmp"/
  printf '#!/usr/bin/env bash\n# summary: x\nvws_cmd_main() { :; }\n' > "$tmp/scripts/vws.d/orphan.sh"
  ! vws_layout_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R17: a command without test and doc was accepted"; }
  : > "$tmp/tests/vws/orphan.test.sh"
  ! vws_layout_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R17: a command without a doc entry was accepted"; }
  printf '### vws orphan\n' >> "$tmp/docs/vws-cli.md"
  vws_layout_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R17: a complete command was rejected"; }
  printf '#!/usr/bin/env bash\n# summary: x\nvws_cmd_main() {\n  kubectl get nodes\n}\n' > "$tmp/scripts/vws.d/orphan.sh"
  ! vws_layout_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R17: a direct kubectl call was accepted"; }
  rm -f "$tmp/scripts/vws.d/orphan.sh"
  printf '#!/usr/bin/env bash\n# no summary\nvws_cmd_main() { :; }\n' > "$tmp/scripts/vws.d/nosummary.sh"
  ! vws_layout_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R17: a command without a summary was accepted"; }
  rm -rf "$tmp"
}

# crd_check ROOT: the only VWS CRD is VWSCluster in charts/vws/crds/vwsclusters.yaml, its schema has no
# secret-shaped field name, and its API group is written there and in the example CRs only.
crd_check() {
  python3 - "$1" <<'PY'
import importlib.util
import re
import sys
from pathlib import Path

import yaml

root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("render", root / "scripts/vws.d/lib/render.py")
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)
crd_path = root / "charts/vws/crds/vwsclusters.yaml"
skip = {".git", "build", ".cache", "node_modules"}
files = [p for p in root.rglob("*") if p.is_file() and not skip & set(p.relative_to(root).parts)]
crds = [p for p in files if p.suffix in (".yaml", ".yml") and re.search(r"^kind:\s*CustomResourceDefinition\s*$", p.read_text(errors="ignore"), re.M)]
if [p.relative_to(root) for p in crds] != [crd_path.relative_to(root)]:
    sys.exit(f"R18: the only VWS CRD is {crd_path.relative_to(root)}, found {[str(p.relative_to(root)) for p in crds]}")
docs = [d for d in yaml.safe_load_all(crd_path.read_text()) if d]
if len(docs) != 1 or docs[0]["spec"]["names"]["kind"] != "VWSCluster":
    sys.exit("R18: vwsclusters.yaml must hold one CRD, kind VWSCluster")
group = docs[0]["spec"]["group"]
crd = docs[0]
if crd["metadata"]["name"] != f"vwsclusters.{group}":
    sys.exit("R18: the CRD name must be vwsclusters.<spec.group>")
# The kubeconform catalog has no CustomResourceDefinition schema, so the structural rules the API
# server enforces are checked here: apiextensions v1, one storage version, a type on every node.
if crd.get("apiVersion") != "apiextensions.k8s.io/v1" or crd["spec"].get("scope") != "Namespaced":
    sys.exit("R18: the CRD must be apiextensions.k8s.io/v1 and Namespaced")
if [v["name"] for v in crd["spec"]["versions"] if v.get("storage")] != ["v1alpha1"]:
    sys.exit("R18: v1alpha1 must be the one storage version")

def structural(schema, path):
    if not schema.get("x-kubernetes-preserve-unknown-fields") and "type" not in schema:
        sys.exit(f"R18: CRD schema node {path or '/'} has no type (not structural)")
    for key, sub in (schema.get("properties") or {}).items():
        structural(sub, f"{path}.{key}")
    for key in ("additionalProperties", "items"):
        if isinstance(schema.get(key), dict):
            structural(schema[key], path + "[*]")
    # The API server rejects additionalProperties beside properties in any form, false included
    # (vws-deploy#81). Unknown fields are refused by render.py and pruned by the API server.
    if schema.get("properties") and "additionalProperties" in schema:
        sys.exit(f"R18: CRD schema node {path} has both properties and additionalProperties")

for version in crd["spec"]["versions"]:
    structural(version["schema"]["openAPIV3Schema"], "")

def walk(schema, path):
    for key, sub in (schema.get("properties") or {}).items():
        if key not in render.REFERENCE_NAMES and render.SECRET_NAME.search(key):
            sys.exit(f"R18: CR schema field {path}.{key} looks like a secret; the CR holds Secret names only")
        walk(sub, f"{path}.{key}")
    for key in ("additionalProperties", "items"):
        if isinstance(schema.get(key), dict):
            walk(schema[key], path + "[*]")

for version in docs[0]["spec"]["versions"]:
    walk(version["schema"]["openAPIV3Schema"], "")
examples = sorted((root / "examples/vwscluster").glob("*.yaml"))
for p in files:
    if p == crd_path or p in examples:
        continue
    if group in p.read_text(errors="ignore"):
        sys.exit(f"R18: the API group {group} is written in {p.relative_to(root)}; read it from {crd_path.relative_to(root)}")
for p in examples:
    if yaml.safe_load(p.read_text()).get("apiVersion") != f"{group}/v1alpha1":
        sys.exit(f"R18: {p.relative_to(root)} apiVersion is not {group}/v1alpha1")
PY
}

cmd_crd() {
  local tmp
  crd_check . || die "R18: VWSCluster CRD rules"
  tmp=$(mktemp -d)
  mkdir -p "$tmp/charts/vws" "$tmp/scripts/vws.d/lib"
  cp -r charts/vws/crds "$tmp/charts/vws/"
  cp scripts/vws.d/lib/render.py "$tmp/scripts/vws.d/lib/"
  cp -r examples "$tmp/"
  crd_check "$tmp" || { rm -rf "$tmp"; die "R18: a clean copy was rejected"; }
  sed 's/vwsclusters/vwsthings/; s/kind: VWSCluster/kind: VWSThing/' charts/vws/crds/vwsclusters.yaml > "$tmp/charts/vws/crds/second.yaml"
  ! crd_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R18: a second CRD was accepted"; }
  rm "$tmp/charts/vws/crds/second.yaml"
  sed 's/^                version:$/                adminPassword:\n                  type: string\n                version:/' charts/vws/crds/vwsclusters.yaml > "$tmp/charts/vws/crds/vwsclusters.yaml"
  grep -q adminPassword "$tmp/charts/vws/crds/vwsclusters.yaml" || { rm -rf "$tmp"; die "R18: negative test setup failed"; }
  ! crd_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R18: a secret field in the CR schema was accepted"; }
  cp charts/vws/crds/vwsclusters.yaml "$tmp/charts/vws/crds/"
  python3 - "$tmp/charts/vws/crds/vwsclusters.yaml" <<'PY'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
d["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["profile"].pop("type")
yaml.safe_dump(d, open(sys.argv[1], "w"))
PY
  ! crd_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R18: a schema node without a type was accepted"; }
  cp charts/vws/crds/vwsclusters.yaml "$tmp/charts/vws/crds/"
  python3 - "$tmp/charts/vws/crds/vwsclusters.yaml" <<'PY'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
d["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["additionalProperties"] = False
yaml.safe_dump(d, open(sys.argv[1], "w"))
PY
  ! crd_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R18: additionalProperties: false beside properties was accepted"; }
  cp charts/vws/crds/vwsclusters.yaml "$tmp/charts/vws/crds/"
  printf 'group: %s\n' "$(sed -n 's/^  group: //p' charts/vws/crds/vwsclusters.yaml)" > "$tmp/scripts/group.txt"
  ! crd_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R18: the API group written in a second place was accepted"; }
  rm "$tmp/scripts/group.txt"
  sed -i 's#^apiVersion: .*#apiVersion: example.com/v1alpha1#' "$tmp/examples/vwscluster/ha.yaml"
  ! crd_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R18: an example CR with another group was accepted"; }
  rm -rf "$tmp"
}

# profile_keys FILE...: fail unless every profile sets the same key paths.
profile_keys() {
  python3 - "$@" <<'PY'
import sys
import yaml

def paths(node, prefix=""):
    if isinstance(node, dict):
        out = set()
        for key, value in node.items():
            out |= paths(value, f"{prefix}.{key}")
        return out
    return {prefix}

sets = {path: paths(yaml.safe_load(open(path))) for path in sys.argv[1:]}
first = next(iter(sets.values()))
for path, keys in sets.items():
    if keys != first:
        sys.exit(f"R19: {path} sets different keys: {sorted(keys ^ first)}")
PY
}

cmd_profiles() {
  local tmp profile version size
  version=$(sed -n 's/^KUBERNETES_VERSION=//p' scripts/versions.env)
  tmp=$(mktemp -d)
  profile_keys profiles/*.yaml || die "R19: profiles must set the same keys"
  for profile in single-node small ha; do
    [[ -f profiles/${profile}.yaml ]] || die "R19: profiles/${profile}.yaml is missing"
    helm lint charts/vws --strict -f "profiles/${profile}.yaml" >/dev/null || die "R19: helm lint fails with ${profile}"
    helm template vws charts/vws --namespace vws -f "profiles/${profile}.yaml" > "$tmp/${profile}.yaml"
    kubeconform -strict -kubernetes-version "$version" -cache .cache/kubeconform "$tmp/${profile}.yaml" || die "R19: kubeconform rejects ${profile}"
    python3 scripts/check_render.py "$tmp/${profile}.yaml" charts/vws/values.yaml vws "profiles/${profile}.yaml" || die "R19: check_render rejects ${profile}"
    # The example CR of the profile renders, passes the same checks, and renders to the same bytes twice.
    scripts/vws render "examples/vwscluster/${profile}.yaml" "$tmp/${profile}.values.yaml" >/dev/null || die "R19: vws render fails on examples/vwscluster/${profile}.yaml"
    scripts/vws render "examples/vwscluster/${profile}.yaml" "$tmp/${profile}.again.yaml" >/dev/null
    cmp -s "$tmp/${profile}.values.yaml" "$tmp/${profile}.again.yaml" || die "R19: vws render is not deterministic for ${profile}"
    helm template vws charts/vws --namespace vws -f "$tmp/${profile}.values.yaml" > "$tmp/${profile}.cr.yaml"
    python3 scripts/check_render.py "$tmp/${profile}.cr.yaml" charts/vws/values.yaml vws "$tmp/${profile}.values.yaml" || die "R19: check_render rejects the render of the ${profile} example"
  done
  grep -q 'whenUnsatisfiable: DoNotSchedule' "$tmp/ha.yaml" || die "R19: profile ha does not require spreading"
  ! grep -q 'whenUnsatisfiable: DoNotSchedule' "$tmp/single-node.yaml" || die "R19: profile single-node requires spreading over one node"
  # spec.storage.s3.rgw of the CR reaches the storage flags and the secretKeyRef env (#108); without it the backend stays off.
  ! grep -q -e '-object-backend=' "$tmp/small.cr.yaml" || die "R19: a CR without s3.rgw turns the object backend on"
  sed -e 's/    storageClass: fast-rbd/    storageClass: fast-rbd\n    s3:\n      rgw: {enabled: true, endpoint: "http:\/\/rgw:8080", bucketPrefix: lab, existingSecret: lab-rgw}/' \
    examples/vwscluster/small.yaml > "$tmp/rgw.cr.in.yaml"
  scripts/vws render "$tmp/rgw.cr.in.yaml" "$tmp/rgw.values.yaml" >/dev/null || die "R19: vws render fails on a CR with s3.rgw"
  helm template vws charts/vws --namespace vws -f "$tmp/rgw.values.yaml" > "$tmp/rgw.cr.yaml"
  python3 scripts/check_render.py "$tmp/rgw.cr.yaml" charts/vws/values.yaml vws "$tmp/rgw.values.yaml" || die "R19: check_render rejects a CR with s3.rgw"
  for want in -object-backend=s3 -s3-endpoint=http://rgw:8080 -s3-bucket-prefix=lab; do
    grep -q -e "- ${want}\$" "$tmp/rgw.cr.yaml" || die "R19: a CR with s3.rgw does not render ${want}"
  done
  grep -q -e 'name: lab-rgw' "$tmp/rgw.cr.yaml" || die "R19: a CR with s3.rgw does not reference existingSecret"
  # Ceph overlays, rendered by scripts/platform.sh.
  for profile in single-node ha; do
    [[ -f build/platform-profiles/31-rook-ceph-cluster.${profile}.yaml ]] || die "R19: run scripts/platform.sh first"
  done
  size=$(python3 -c 'import sys,yaml;print(sorted({p["spec"]["replicated"]["size"] for d in yaml.safe_load_all(open(sys.argv[1])) if d and d["kind"]=="CephBlockPool" for p in [d]}))' build/platform-profiles/31-rook-ceph-cluster.single-node.yaml)
  [[ $size == "[1]" ]] || die "R19: single-node Ceph block pool size is ${size}, want [1]"
  size=$(python3 -c 'import sys,yaml;print(sorted({p["spec"]["replicated"]["size"] for d in yaml.safe_load_all(open(sys.argv[1])) if d and d["kind"]=="CephBlockPool" for p in [d]}))' build/platform-profiles/31-rook-ceph-cluster.ha.yaml)
  [[ $size == "[3]" ]] || die "R19: ha Ceph block pool size is ${size}, want [3]"
  # Negative: a profile with one more key must fail the key check.
  cp profiles/*.yaml "$tmp/"
  python3 - "$tmp/small.yaml" <<'PY'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
d["global"]["pendingFlags"] = True
yaml.safe_dump(d, open(sys.argv[1], "w"))
PY
  ! profile_keys "$tmp/single-node.yaml" "$tmp/small.yaml" "$tmp/ha.yaml" 2>/dev/null || { rm -rf "$tmp"; die "R19: a profile with an extra key was accepted"; }
  rm -rf "$tmp"
}

# bootstrap_check FILE TTL: the rendered bootstrap (scripts/bootstrap.sh) keeps the installer rules (R20).
bootstrap_check() {
  python3 - "$1" "$2" <<'PY'
import sys
import yaml

docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
ttl = int(sys.argv[2])
by = {(d["kind"], d["metadata"]["name"]): d for d in docs}

def fail(msg):
    sys.exit(f"R20: {msg}")

text = open(sys.argv[1]).read()
for bad in ("privileged: true", "hostPath:", "hostNetwork: true", "hostPID: true", "hostIPC: true"):
    if bad in text:
        fail(f"bootstrap.yaml has {bad}")
pods = [d["spec"] for d in docs if d["kind"] == "Pod"] + [d["spec"]["template"]["spec"] for d in docs if d["kind"] == "Job"]
for spec in pods:
    for c in spec["containers"]:
        if "@sha256:" not in c["image"]:
            fail(f"image {c['image']} is not pinned by digest")
        if (c.get("securityContext") or {}).get("allowPrivilegeEscalation") is not False:
            fail(f"container {c['name']} allows privilege escalation")
admin = [d for d in docs if d["kind"] == "ClusterRoleBinding" and d["roleRef"]["name"] == "cluster-admin"]
if [d["metadata"]["name"] for d in admin] != ["vws-installer-temp"]:
    fail("cluster-admin is bound only by vws-installer-temp")
if int((admin[0]["metadata"].get("annotations") or {}).get("vws-installer-ttl-seconds", "0")) != ttl:
    fail("vws-installer-temp has no vws-installer-ttl-seconds annotation equal to INSTALLER_TTL_SECONDS")
pod = by.get(("Pod", "vws-installer"))
if pod is None or pod["spec"].get("activeDeadlineSeconds") != ttl:
    fail("the installer pod must end at INSTALLER_TTL_SECONDS (activeDeadlineSeconds)")
reaper = by.get(("Job", "vws-installer-reaper"))
if reaper is None:
    fail("the reaper Job that deletes vws-installer-temp at its TTL is missing")
if reaper["spec"].get("activeDeadlineSeconds", 0) <= ttl:
    fail("the reaper deadline must be longer than the TTL, or it is killed before its delete")
if "delete clusterrolebinding vws-installer-temp" not in reaper["spec"]["template"]["spec"]["containers"][0]["args"][0]:
    fail("the reaper does not delete vws-installer-temp")
for d in docs:
    if d["kind"] in ("Role", "ClusterRole"):
        for rule in d["rules"]:
            if "*" in rule["verbs"] or "*" in rule["resources"]:
                fail(f"{d['kind']} {d['metadata']['name']} uses a wildcard")
            if "secrets" in rule["resources"]:
                fail(f"{d['kind']} {d['metadata']['name']} keeps access to secrets")
            if set(rule.get("apiGroups", [])) & {"rbac.authorization.k8s.io"}:
                fail(f"{d['kind']} {d['metadata']['name']} keeps RBAC rights")
            if d["kind"] == "ClusterRole" and set(rule["verbs"]) - {"get", "list"}:
                fail(f"ClusterRole {d['metadata']['name']} writes cluster objects after finish")
# The operating ClusterRole holds exactly this list (vws-deploy#77): group, resource, get and list only.
OPERATOR_READ = {
    ("", "nodes"), ("storage.k8s.io", "storageclasses"), ("snapshot.storage.k8s.io", "volumesnapshotclasses"),
    ("apiextensions.k8s.io", "customresourcedefinitions"), ("kubevirt.io", "kubevirts"), ("cdi.kubevirt.io", "cdis"),
    ("ceph.rook.io", "cephclusters"), ("", "persistentvolumeclaims"),
}
read = by.get(("ClusterRole", "vws-operator-read"))
if read is None:
    fail("the ClusterRole vws-operator-read is missing")
have = set()
for rule in read["rules"]:
    if sorted(rule["verbs"]) != ["get", "list"]:
        fail(f"vws-operator-read rule {rule['resources']} has verbs {rule['verbs']}; only get and list")
    if rule.get("resourceNames") or rule.get("nonResourceURLs"):
        fail("vws-operator-read uses resourceNames or nonResourceURLs")
    for group in rule["apiGroups"]:
        for resource in rule["resources"]:
            if (group, resource) not in OPERATOR_READ:
                fail(f"vws-operator-read grants {group or 'core'}/{resource}, which is not in the allowed list (docs/rbac.md)")
            have.add((group, resource))
if have != OPERATOR_READ:
    fail(f"vws-operator-read lacks {sorted(OPERATOR_READ - have)}")
PY
}

cmd_bootstrap() {
  local tmp ttl
  scripts/bootstrap.sh >/dev/null || die "R20: scripts/bootstrap.sh fails"
  ttl=$(sed -n 's/^INSTALLER_TTL_SECONDS=//p' scripts/versions.env)
  bootstrap_check build/bootstrap.yaml "$ttl" || die "R20: bootstrap rules"
  tmp=$(mktemp -d)
  bad_bootstrap() { # NAME SED
    sed -e "$2" build/bootstrap.yaml > "$tmp/b.yaml"
    cmp -s build/bootstrap.yaml "$tmp/b.yaml" && { rm -rf "$tmp"; die "R20: negative test '$1' changed nothing"; }
    ! bootstrap_check "$tmp/b.yaml" "$ttl" 2>/dev/null || { rm -rf "$tmp"; die "R20: $1 was accepted"; }
  }
  bad_bootstrap "a tag instead of a digest" 's#installer@sha256:[0-9a-f]*#installer:latest#'
  bad_bootstrap "a privileged container" 's#^\( *\)allowPrivilegeEscalation: false#\1allowPrivilegeEscalation: false\n\1privileged: true#'
  bad_bootstrap "privilege escalation" '0,/allowPrivilegeEscalation: false/s//allowPrivilegeEscalation: true/'
  bad_bootstrap "a binding without a TTL" '/vws-installer-ttl-seconds: /d'
  bad_bootstrap "a pod without a deadline" "0,/activeDeadlineSeconds: ${ttl}\$/s///"
  bad_bootstrap "no reaper" 's/name: vws-installer-reaper/name: something-else/'
  bad_bootstrap "a reaper deadline equal to the TTL" "s/activeDeadlineSeconds: $((ttl + 600))/activeDeadlineSeconds: ${ttl}/"
  bad_bootstrap "a second cluster-admin binding" 's/name: vws-operator-read$/name: cluster-admin/'
  bad_bootstrap "the operating Role reading secrets" 's/resources: \[pods, /resources: [secrets, pods, /'
  bad_bootstrap "a wildcard rule" 's/resources: \[nodes\]/resources: ["*"]/'
  bad_bootstrap "watch on kubevirts" '/resources: \[kubevirts\]/{n;s/verbs: \[get, list\]/verbs: [get, list, watch]/}'
  bad_bootstrap "a resource outside the list" 's/resources: \[kubevirts\]/resources: [kubevirts, virtualmachines]/'
  bad_bootstrap "a write verb on PVCs" '/resources: \[persistentvolumeclaims\]$/{n;s/verbs: \[get, list\]/verbs: [get, list, delete]/}'
  bad_bootstrap "a group outside the list" 's/apiGroups: \[ceph.rook.io\]/apiGroups: [ceph.rook.io, objectbucket.io]/'
  bad_bootstrap "pods cluster-wide" 's/resources: \[cdis\]/resources: [cdis, pods]/'
  bad_bootstrap "an allowed read removed" '/resources: \[cephclusters\]/s/cephclusters/cephblockpools/'
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
  cmd_vws
  cmd_bootstrap
  cmd_crd
  cmd_bundle
  cmd_profiles
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
usage: guard.sh <layout|names|assets|ci|cursor|agents|bundle|config|vws|bootstrap|crd|profiles|schema|hooks|docs|recovery|commits|commit-msg|all>
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
  vws) cmd_vws ;;
  bootstrap) cmd_bootstrap ;;
  crd) cmd_crd ;;
  profiles) cmd_profiles ;;
  schema) cmd_schema ;;
  hooks) cmd_hooks ;;
  docs) cmd_docs ;;
  recovery) cmd_recovery ;;
  commits) cmd_commits "$@" ;;
  commit-msg) cmd_commit_msg "$@" ;;
  all) cmd_all ;;
  *) usage; exit 2 ;;
esac
