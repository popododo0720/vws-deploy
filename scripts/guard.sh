#!/usr/bin/env bash
# Repo checks for the rules in AGENTS.md.
set -euo pipefail
export LC_ALL=C

ROOT=$(CDPATH= cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Site-only values (the elastic IP pool) that every helm call of these checks needs.
SITE_VALUES=charts/vws/ci/site.yaml
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
  if find . -path ./.git -prune -o -path ./packaging -prune -o \( -name '*.service' -o -name '*.target' \) -print | grep -q .; then
    die "R10: systemd units live only under packaging/"
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
import re
import sys
from pathlib import Path

endpoint_url = re.compile(r"https://[a-z0-9.{}-]+\.am[a]zonaws\.com\b")
assert endpoint_url.sub("", "https://ec2.{region}.ama" + "zonaws.com") == ""
notice = "ama" + "zon is a trade" + "mark of"
assert endpoint_url.sub("", notice) == notice
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
    text = endpoint_url.sub("", path.read_text(errors="ignore").lower())
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
    --exclude=crd-check.sh "$@"
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
  if grep -R -n -w -E 'helm|kubeconform' .github/workflows; then
    die "R8: workflows must not run the retired chart validators"
  fi
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
single = {"console-api", "iam"}
stateless = next((name for name, spec in services.items() if "state" not in (spec or {}) and name not in single), None)
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
        ["helm", "template", "vws", "charts/vws", "-f", "charts/vws/ci/site.yaml", "-f", handle.name],
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
        print("R5: helm template failed for a reason other than the values schema", file=sys.stderr)
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
  local hook repo base next side zero=0000000000000000000000000000000000000000
  hook="$PWD/.githooks/pre-push"
  repo=$(mktemp -d)
  git -C "$repo" init -q
  git -C "$repo" -c user.name=t -c user.email=t@t commit -q --allow-empty -m base
  base=$(git -C "$repo" rev-parse HEAD)
  git -C "$repo" -c user.name=t -c user.email=t@t commit -q --allow-empty -m next
  next=$(git -C "$repo" rev-parse HEAD)
  git -C "$repo" checkout -q "$base"
  git -C "$repo" -c user.name=t -c user.email=t@t commit -q --allow-empty -m side
  side=$(git -C "$repo" rev-parse HEAD)
  (cd "$repo" && printf '%s\n' "refs/heads/topic $next refs/heads/topic $base" | "$hook" >/dev/null) ||
    { rm -rf "$repo"; die "R1: pre-push refused a fast-forward push"; }
  (cd "$repo" && printf '%s\n' "refs/heads/topic $next refs/heads/topic $zero" | "$hook" >/dev/null) ||
    { rm -rf "$repo"; die "R1: pre-push refused a new branch"; }
  (cd "$repo" && printf '%s\n' "(delete) $zero refs/heads/topic $next" | "$hook" >/dev/null) ||
    { rm -rf "$repo"; die "R1: pre-push refused a branch deletion"; }
  (cd "$repo" && printf '%s\n' "refs/heads/topic $side refs/heads/topic $next" | "$hook" >/dev/null 2>&1) ||
    { rm -rf "$repo"; die "R1: pre-push refused a non-fast-forward push of a topic branch whose old head exists locally"; }
  set +e
  (cd "$repo" && printf '%s\n' "refs/heads/topic $next refs/heads/topic 1111111111111111111111111111111111111111" | "$hook" >/dev/null 2>&1)
  rc=$?
  set -e
  [[ $rc -ne 0 ]] || { rm -rf "$repo"; die "R1: pre-push accepted a push over an unknown remote head"; }
  set +e
  (cd "$repo" && printf '%s\n' "refs/heads/main $side refs/heads/main $next" | "$hook" >/dev/null 2>&1)
  rc=$?
  set -e
  [[ $rc -ne 0 ]] || { rm -rf "$repo"; die "R1: pre-push accepted a non-fast-forward push to main"; }
  rm -rf "$repo"
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
for heading in ("## Rules", "## Branches and atomic commits", "## Commands"):
    if heading not in text:
        print(f"R2: AGENTS.md is missing {heading}", file=sys.stderr)
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
  scripts/guard-agents.sh commit-msg "$file" || die "R13: a commit must not close an issue; write 'Part of #N'"
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
  helm template "$release" charts/vws -f "$SITE_VALUES" --namespace "$release" > "$rendered"
  python3 scripts/check_render.py "$rendered" charts/vws/values.yaml "$release" "$SITE_VALUES"
  rm -f "$rendered"
}

# The negative half of R24: one production render per placeholder must fail and name its key.
placeholder_case() {
  local tmp=$1 name=$2 path=$3 value=$4 expect="$3: $4 is a placeholder"
  python3 - "$tmp/site.yaml" "$path" "$value" > "$tmp/case.yaml" <<'PY'
import sys
import yaml

values = yaml.safe_load(open(sys.argv[1]))
parts = sys.argv[2].split(".")
node = values
for part in parts[:-1]:
    node = node.setdefault(part, {})
node[parts[-1]] = sys.argv[3]
yaml.safe_dump(values, sys.stdout)
PY
  helm template "${VWS_RELEASE:-vws}" charts/vws --namespace "${VWS_RELEASE:-vws}" -f "$tmp/case.yaml" > "$tmp/case.render.yaml"
  if python3 scripts/check_render.py --profile production "$tmp/case.render.yaml" charts/vws/values.yaml "${VWS_RELEASE:-vws}" "$tmp/case.yaml" > "$tmp/out" 2>&1; then
    die "R24: production accepted ${name}"
  fi
  grep -Fq -- "$expect" "$tmp/out" || { cat "$tmp/out" >&2; die "R24: ${name} did not name ${expect}"; }
}

# placeholder_pass TMP NAME PATH VALUE: production must accept VALUE at PATH (no false positive).
placeholder_pass() {
  local tmp=$1 name=$2 path=$3 value=$4
  python3 - "$tmp/site.yaml" "$path" "$value" > "$tmp/pass.yaml" <<'PY'
import sys
import yaml

values = yaml.safe_load(open(sys.argv[1]))
parts = sys.argv[2].split(".")
node = values
for part in parts[:-1]:
    node = node.setdefault(part, {})
node[parts[-1]] = sys.argv[3]
yaml.safe_dump(values, sys.stdout)
PY
  helm template "${VWS_RELEASE:-vws}" charts/vws --namespace "${VWS_RELEASE:-vws}" -f "$tmp/pass.yaml" > "$tmp/pass.render.yaml"
  python3 scripts/check_render.py --profile production "$tmp/pass.render.yaml" charts/vws/values.yaml "${VWS_RELEASE:-vws}" "$tmp/pass.yaml" > "$tmp/out" 2>&1 \
    || { cat "$tmp/out" >&2; die "R24: production rejected ${name}"; }
}

cmd_placeholders() {
  local profile=production tmp
  local -a files=()
  while (($#)); do
    case "$1" in
      --profile) (($# >= 2)) || die "R24: --profile needs a value"; profile=$2; shift 2 ;;
      --profile=*) profile=${1#*=}; shift ;;
      *) files+=("$1"); shift ;;
    esac
  done
  case "$profile" in
    dev | production) ;;
    *) die "R24: unknown --profile '${profile}'; expected one of: dev, production" ;;
  esac
  tmp=$(mktemp -d)
  if ((${#files[@]})); then
    local -a value_flags=()
    local file
    for file in "${files[@]}"; do value_flags+=(-f "$file"); done
    helm template "${VWS_RELEASE:-vws}" charts/vws --namespace "${VWS_RELEASE:-vws}" "${value_flags[@]}" > "$tmp/input.render.yaml"
    python3 scripts/check_render.py --profile "$profile" "$tmp/input.render.yaml" charts/vws/values.yaml "${VWS_RELEASE:-vws}" "${files[@]}" \
      || { rm -rf "$tmp"; die "R24: the ${profile} render has a placeholder"; }
    rm -rf "$tmp"
    return
  fi
  cat > "$tmp/site.yaml" <<'EOF'
services:
  network:
    flags:
      pool: 10.20.0.0/24
  console-api:
    flags:
      console-origins: https://console.vws.internal
EOF
  helm template "${VWS_RELEASE:-vws}" charts/vws --namespace "${VWS_RELEASE:-vws}" -f "$tmp/site.yaml" > "$tmp/site.render.yaml"
  python3 scripts/check_render.py --profile production "$tmp/site.render.yaml" charts/vws/values.yaml "${VWS_RELEASE:-vws}" "$tmp/site.yaml" \
    || { rm -rf "$tmp"; die "R24: production rejected the site values"; }

  placeholder_case "$tmp" "RFC 5737 192.0.2.0/24" services.network.flags.pool 192.0.2.0/24
  placeholder_case "$tmp" "an address in 192.0.2.0/24" services.network.flags.pool 192.0.2.17
  placeholder_case "$tmp" "RFC 5737 198.51.100.0/24" services.network.flags.pool 198.51.100.0/24
  placeholder_case "$tmp" "an address in 198.51.100.0/24" services.network.flags.pool 198.51.100.42
  placeholder_case "$tmp" "RFC 5737 203.0.113.0/24" services.network.flags.pool 203.0.113.0/24
  placeholder_case "$tmp" "an address in 203.0.113.0/24" services.network.flags.pool 203.0.113.9
  placeholder_case "$tmp" "RFC 3849 2001:db8::/32" services.network.flags.pool 2001:db8::/32
  placeholder_case "$tmp" "an address in 2001:db8::/32" services.network.flags.pool 2001:db8::1
  placeholder_case "$tmp" "the reserved domain example.com" services.console-api.flags.console-origins https://example.com
  placeholder_case "$tmp" "the reserved domain example.org" services.console-api.flags.console-origins https://example.org
  placeholder_case "$tmp" "the reserved domain example.net" services.console-api.flags.console-origins https://example.net
  placeholder_case "$tmp" "the reserved TLD .example" services.console-api.flags.console-origins https://app.example
  placeholder_case "$tmp" "the reserved TLD .invalid" services.console-api.flags.console-origins https://console.example.invalid
  placeholder_case "$tmp" "the reserved TLD .test" services.console-api.flags.console-origins https://app.test
  placeholder_case "$tmp" "the reserved TLD .localhost" services.console-api.flags.console-origins https://app.localhost
  placeholder_case "$tmp" "the bare host localhost in a URL" services.console-api.flags.console-origins https://localhost
  placeholder_case "$tmp" "the bare host localhost with a port" services.console-api.flags.console-origins https://localhost:8443
  placeholder_case "$tmp" "the bare flag value localhost" services.console-api.flags.console-origins localhost
  placeholder_case "$tmp" "the trailing-dot reserved TLD .invalid" services.console-api.flags.console-origins https://console.example.invalid.
  placeholder_case "$tmp" "the trailing-dot reserved TLD .test" services.console-api.flags.console-origins https://x.test.
  placeholder_case "$tmp" "the trailing-dot reserved domain example.com" services.console-api.flags.console-origins https://example.com.
  placeholder_case "$tmp" "the trailing-dot bare host localhost" services.console-api.flags.console-origins localhost.
  placeholder_case "$tmp" "the display string CHANGE_ME" services.network.flags.pool CHANGE_ME
  placeholder_case "$tmp" "the display string changeme" services.network.flags.pool changeme
  placeholder_case "$tmp" "the display string TODO" services.network.flags.pool TODO
  placeholder_case "$tmp" "an angle-bracket placeholder" services.network.flags.pool '<site pool>'

  # Legitimate production values pass: a private pool and a real FQDN, with and without the
  # root dot, and names that only contain a reserved label (vws-deploy#629 AC5).
  placeholder_pass "$tmp" "a real FQDN with a trailing dot" services.console-api.flags.console-origins https://console.vws.internal.
  placeholder_pass "$tmp" "a real FQDN with a trailing dot and a port" services.console-api.flags.console-origins https://console.vws.internal.:8443
  placeholder_pass "$tmp" "a host whose first label is localhost" services.console-api.flags.console-origins https://localhost.vws.internal
  placeholder_pass "$tmp" "a host whose label starts with localhost" services.console-api.flags.console-origins https://localhost-proxy.vws.internal
  placeholder_pass "$tmp" "the private pool 10.20.0.0/24" services.network.flags.pool 10.20.0.0/24

  # A development render keeps bare localhost (vws-deploy#629 AC3).
  placeholder_case "$tmp" "the bare host localhost" services.console-api.flags.console-origins https://localhost
  python3 scripts/check_render.py --profile dev "$tmp/case.render.yaml" charts/vws/values.yaml "${VWS_RELEASE:-vws}" "$tmp/case.yaml" >/dev/null \
    || { rm -rf "$tmp"; die "R24: the development profile rejected localhost"; }

  # Only dev and production are profiles; anything else fails closed (vws-deploy#629 AC1, AC2).
  local bad
  for bad in prod Production staging ''; do
    if python3 scripts/check_render.py --profile "$bad" "$tmp/site.render.yaml" charts/vws/values.yaml "${VWS_RELEASE:-vws}" "$tmp/site.yaml" > "$tmp/out" 2>&1; then
      rm -rf "$tmp"; die "R24: check_render.py accepted --profile '${bad}'"
    fi
    grep -Fq "unknown --profile '${bad}'" "$tmp/out" || { cat "$tmp/out" >&2; rm -rf "$tmp"; die "R24: --profile '${bad}' did not name the profile"; }
    if (cmd_placeholders --profile "$bad") > "$tmp/out" 2>&1; then
      rm -rf "$tmp"; die "R24: guard.sh placeholders accepted --profile '${bad}'"
    fi
    grep -Fq "unknown --profile '${bad}'" "$tmp/out" || { cat "$tmp/out" >&2; rm -rf "$tmp"; die "R24: guard.sh placeholders --profile '${bad}' did not name the profile"; }
  done

  # The same placeholder stays legal in a development or lab render (AC3).
  python3 scripts/check_render.py "$tmp/case.render.yaml" charts/vws/values.yaml "${VWS_RELEASE:-vws}" "$tmp/case.yaml" >/dev/null \
    || { rm -rf "$tmp"; die "R24: the default development render rejected a placeholder"; }
  python3 scripts/check_render.py --profile dev "$tmp/case.render.yaml" charts/vws/values.yaml "${VWS_RELEASE:-vws}" "$tmp/case.yaml" >/dev/null \
    || { rm -rf "$tmp"; die "R24: the development profile rejected a placeholder"; }

  rm -rf "$tmp"
}

cmd_cursor() {
  [[ ! -e .cursor ]] || die "R9: .cursor/ does not belong in this repo"
  if grep -R -n -i -E 'api\.cursor\.com|cursor-agent|cursor cloud|CloudAgent' .github Makefile scripts charts 2>/dev/null \
    | grep -v 'scripts/guard.sh:'; then
    die "R9: Cursor cloud agents must not be called from this repo"
  fi
  grep -q 'Do not use Cursor cloud agents' AGENTS.md || die "R9: AGENTS.md lost the Cursor rule"
}

cmd_bundle() {
  if grep -nE '^(KUBEVIRT|CDI|MULTUS|KUBEOVN|ROOK|CSI_DRIVER_NFS|KUBECONFORM)_' scripts/versions.env docs/image-pins.md; then
    die "R12: retired Kubernetes bundle and validator pins must not return"
  fi
}

cmd_agents() {
  # The jobs live in one reusable workflow per job; repo-guard.yml is their short caller (#402).
  local rg_dir=.github/workflows rg_hy=.github/workflows/repo-guard-pr-hygiene.yml rg_vg=.github/workflows/repo-guard-verify-gate.yml f
  local rg_all=("$rg_dir/repo-guard.yml" "$rg_hy" "$rg_vg" "$rg_dir/repo-guard-verify-reset.yml" "$rg_dir/repo-guard-verify-rerun.yml")
  for f in "${rg_all[@]:1}"; do
    [[ -f $f ]] || die "R13: ${f} is missing; the shared repo-guard jobs live there, one per file (#402)"
    grep -q -x '  workflow_call:' "$f" || die "R13: ${f} must be a reusable workflow (on: workflow_call)"
  done
  python3 .github/drift/caller_pin.py --home .github/workflows/repo-guard.yml >/dev/null \
    || die "R13: .github/workflows/repo-guard.yml must be the caller of the repo-guard job files (.github/drift/caller_pin.py, #402)"
  python3 -m unittest discover -s tests/workflows >/dev/null 2>&1 \
    || die "R13: tests/workflows fail (python3 -m unittest discover -s tests/workflows -v)"
  python3 -m unittest discover -s .github/drift >/dev/null 2>&1 \
    || die "R13: a workflow uses an action not pinned by full SHA (python3 -m unittest discover -s .github/drift -v, #700)"
  scripts/guard-agents.sh agents AGENTS.md || die "R13: AGENTS.md lost the Insider-risk guardrails"
  scripts/guard-agents.sh rules AGENTS.md || die "R13: a row of the AGENTS.md rule table has no enforcer"
  [[ -f installer/internal/archtest/arch_test.go ]] || die "R26: installer/internal/archtest/arch_test.go is missing"
  grep -q -x -F '        run: ../scripts/guard-agents.sh archtest ../AGENTS.md' .github/workflows/ci.yml ||
    die "R26: the ci installer job does not run scripts/guard-agents.sh archtest"
  [[ $(grep -c -E '^def (spec_state|spec_gate)\(' .github/orchestrate/rules/common.py) == 2 ]] ||
    die "R27: .github/orchestrate/rules/common.py lost spec_state() or spec_gate()"
  (cd tests/orchestrate && python3 -m unittest test_spec_first test_decide.SpecFirst >/dev/null 2>&1) ||
    die "R27: the spec-first tests fail (cd tests/orchestrate && python3 -m unittest test_spec_first test_decide.SpecFirst -v)"
  scripts/guard-agents.sh selftest >/dev/null || die "R13: scripts/guard-agents.sh accepted a bad input"
  scripts/guard-agents.sh correct . || die "R21: the /correct loop, incident template or incident label is missing"
  grep -q 'guard-agents.sh pr-base' "$rg_hy" || die "R13: repo-guard-pr-hygiene.yml does not check the base"
  grep -q 'guard-agents.sh pr-body' "$rg_hy" || die "R13: repo-guard-pr-hygiene.yml does not check Part of #N"
  grep -q 'guard-agents.sh pr-checklist' "$rg_hy" || die "R13: repo-guard-pr-hygiene.yml does not check the Quality checklist"
  grep -q '.github/ac/ac-lint.sh' "$rg_hy" || die "R13: repo-guard-pr-hygiene.yml does not lint the linked issue's acceptance criteria"
  (cd scripts && sha256sum --quiet -c guard-agents.sh.sha256) || die "R13: scripts/guard-agents.sh.sha256 does not match scripts/guard-agents.sh; this repo holds the canonical copy, so update the sum in the same commit (cd scripts && sha256sum guard-agents.sh > guard-agents.sh.sha256)"
  grep -q 'guard-agents.sh identity' "$rg_hy" || die "R13: repo-guard-pr-hygiene.yml does not check the guard-agents.sh identity"
  grep -q 'guard-agents.sh pr-commits' "$rg_hy" || die "R13: repo-guard-pr-hygiene.yml does not check that no commit closes an issue"
  grep -q 'guard-agents.sh push-watch' .github/workflows/direct-push-watch.yml || die "R13: direct-push-watch.yml does not watch pushes to main"
  ! grep -q 'push:' "${rg_all[@]}" || die "R13: repo-guard must run on pull requests only, so a pull request has no skipped check"
  grep -q -E 'guard-agents.sh verify-gate( |$)' "$rg_vg" || die "R13: repo-guard-verify-gate.yml has no verify-gate (#249)"
  grep -q -e 'labeled, unlabeled' .github/workflows/repo-guard.yml || die "R13: repo-guard.yml must re-run verify-gate on labeled/unlabeled"
  ! grep -q 'pull_request_target' "${rg_all[@]}" || die "R13: repo-guard must not use pull_request_target"
  grep -q 'ref: ${{ github.event.pull_request.base.ref }}' "$rg_vg" || die "R13: verify-gate must run the guard from the current base branch tip"
  grep -q 'GUARD_VERIFY_EXCLUDE=' "$rg_vg" || die "R13: verify-gate must exclude the PR author and the head pusher"
  grep -q 'GUARD_VERIFY_IDENTITY: ${{ vars.VWS_VERIFY_IDENTITY }}' "$rg_vg" || die "R13: verify-gate must pass the vws-verify identity"
}

console_api_closed() {
  python3 - "$1" <<'PY'
import sys, yaml
path = sys.argv[1]
docs = [d for d in yaml.safe_load_all(open(path)) if d]
svc = next(d for d in docs if d.get("kind") == "Service" and d["metadata"]["name"] == "console-api")
ports = [{"name": p.get("name"), "port": p.get("port"), "targetPort": p.get("targetPort")} for p in svc["spec"]["ports"]]
if ports != [{"name": "http", "port": 8080, "targetPort": "http"}]:
    sys.exit(f"console-api ports {ports}")
text = open(path).read()
for bad in ("-iam=grpc", "-iam-endpoint", "-ec2-addr", "-s3-addr", "-iam-addr"):
    if bad in text:
        sys.exit(f"found {bad}")
PY
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

  helm template vws charts/vws -f "$SITE_VALUES" --namespace vws > "$tmp/default.yaml"
  grep -q -e '- -kube-namespace=vws-vms' "$tmp/default.yaml" || die "R14: storage does not get -kube-namespace from values"
  local f
  for f in -hypervisor=kubevirt -vm-namespace=vws-vms -image-namespace=vws-images -storage=storage:7400; do
    grep -q -e "- ${f}\$" "$tmp/default.yaml" || die "R14: the default render lacks compute ${f} (#121)"
  done
  python3 - "$tmp/default.yaml" <<'PY' || die "R14: compute has no persistentvolumeclaims get in the VM namespace (#121)"
import sys, yaml
ok = any(d and d.get("kind") == "Role" and d["metadata"].get("name") == "vws-compute" and d["metadata"].get("namespace") == "vws-vms"
         and any("" in r.get("apiGroups", []) and "persistentvolumeclaims" in r.get("resources", []) and "get" in r.get("verbs", []) for r in d.get("rules", []))
         for d in yaml.safe_load_all(open(sys.argv[1])))
sys.exit(0 if ok else 1)
PY
  python3 - "$tmp/default.yaml" <<'PY' || die "R14: compute has no virtualmachineinstances get in the VM namespace (#121, vws-compute#50)"
import sys, yaml
ok = any(d and d.get("kind") == "Role" and d["metadata"].get("name") == "vws-compute" and d["metadata"].get("namespace") == "vws-vms"
         and any("kubevirt.io" in r.get("apiGroups", []) and "virtualmachineinstances" in r.get("resources", []) and "get" in r.get("verbs", []) for r in d.get("rules", []))
         for d in yaml.safe_load_all(open(sys.argv[1])))
sys.exit(0 if ok else 1)
PY

  grep -q -e 'name: VWS_HEALTH_ADDR' "$tmp/default.yaml" || die "R15: compute has no VWS_HEALTH_ADDR (#212)"
  grep -q -e 'path: /readyz' "$tmp/default.yaml" || die "R15: compute readiness does not use /readyz (#212)"
  if grep -q -e 'prometheus.io/' "$tmp/default.yaml"; then
    die "R15: compute still carries scrape annotations (#635)"
  fi
  grep -q -e 'tcpSocket' "$tmp/default.yaml" || die "R15: default probes are not tcpSocket"
  python3 - "$tmp/default.yaml" charts/vws/values.yaml <<'PY' || die "R15: HTTP probes must match the services with healthPort (#212) or httpProbes (#612)"
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
values = yaml.safe_load(open(sys.argv[2]))
health = {
    name
    for name, spec in values["services"].items()
    if isinstance(spec, dict) and spec.get("enabled", True) is not False and (spec.get("healthPort") or spec.get("httpProbes"))
}
http = {
    d["metadata"]["name"]
    for d in docs
    if d.get("kind") in ("Deployment", "StatefulSet")
    for c in d["spec"]["template"]["spec"]["containers"]
    if any("httpGet" in c.get(k, {}) for k in ("startupProbe", "livenessProbe", "readinessProbe"))
}
sys.exit(0 if http == health else 1)
PY
  python3 - "$tmp/default.yaml" <<'PY' || die "R15: console probes are not httpGet /healthz and /readyz on port http (#612)"
import sys, yaml
want = {"startupProbe": "/healthz", "livenessProbe": "/healthz", "readinessProbe": "/readyz"}
for d in yaml.safe_load_all(open(sys.argv[1])):
    if d and d.get("kind") == "Deployment" and d["metadata"]["name"] == "console":
        c = d["spec"]["template"]["spec"]["containers"][0]
        got = {k: (c.get(k) or {}).get("httpGet") or {} for k in want}
        sys.exit(0 if all(got[k].get("path") == p and got[k].get("port") == "http" for k, p in want.items()) else 1)
sys.exit(1)
PY
  helm template vws charts/vws -f "$SITE_VALUES" --namespace vws --set global.probe=grpc > "$tmp/grpc.yaml"
  python3 scripts/check_render.py "$tmp/grpc.yaml" charts/vws/values.yaml vws "$SITE_VALUES" <(printf 'global:\n  probe: grpc\n')
  grep -q -e 'grpc:' "$tmp/grpc.yaml" || die "R15: global.probe=grpc does not render gRPC probes"
  ! grep -q -e 'tcpSocket' "$tmp/grpc.yaml" || die "R15: global.probe=grpc left a tcpSocket probe"
  helm template vws charts/vws -f "$SITE_VALUES" --namespace vws --set global.pendingFlags=true > "$tmp/health.yaml"
  # storage readiness is the aggregate (engine, store, kube), never a bare port check (#110, #620).
  for f in default grpc health; do
    python3 - "$tmp/$f.yaml" <<'PY' || die "R15: the $f render's storage readiness is not httpGet /readyz on metrics (#110, #620)"
import sys, yaml
for d in yaml.safe_load_all(open(sys.argv[1])):
    if d and d.get("kind") in ("Deployment", "StatefulSet") and d["metadata"]["name"] == "storage":
        got = (d["spec"]["template"]["spec"]["containers"][0].get("readinessProbe") or {}).get("httpGet") or {}
        sys.exit(0 if got.get("path") == "/readyz" and got.get("port") == "metrics" else 1)
sys.exit(1)
PY
  done
  python3 - "$tmp/default.yaml" <<'PY' || die "R14: storage has no virtualmachines list in the VM namespace (#123, vws-storage#68)"
import sys, yaml
ok = any(d and d.get("kind") == "Role" and d["metadata"].get("name") == "vws-storage" and d["metadata"].get("namespace") == "vws-vms"
         and any("kubevirt.io" in r.get("apiGroups", []) and "virtualmachines" in r.get("resources", []) and r.get("verbs") == ["list"] for r in d.get("rules", []))
         for d in yaml.safe_load_all(open(sys.argv[1])))
sys.exit(0 if ok else 1)
PY
  python3 scripts/check_render.py "$tmp/health.yaml" charts/vws/values.yaml vws "$SITE_VALUES" <(printf 'global:\n  pendingFlags: true\n')
  # vws-iam (#243): off by default (no image yet); on, it gets the KEK by secretKeyRef and a NetworkPolicy.
  ! grep -q 'name: iam$' "$tmp/default.yaml" || die "R16: the default render has vws-iam, which has no image yet"
  helm template vws charts/vws -f "$SITE_VALUES" --namespace vws --set services.iam.enabled=true > "$tmp/iam.yaml"
  python3 scripts/check_render.py "$tmp/iam.yaml" charts/vws/values.yaml vws "$SITE_VALUES" <(printf 'services:\n  iam:\n    enabled: true\n') ||
    die "R16: check_render rejects the vws-iam render"
  # admin (#391): off by default (the installer image is private on GHCR); on, it is loopback, so no Service,
  # a NetworkPolicy for the health port only, and the schema refuses anything that opens it up.
  ! grep -q 'name: admin$' "$tmp/default.yaml" || die "R10: the default render has the admin page, whose image is private"
  helm template vws charts/vws -f "$SITE_VALUES" --namespace vws --set services.admin.enabled=true > "$tmp/admin.yaml"
  python3 scripts/check_render.py "$tmp/admin.yaml" charts/vws/values.yaml vws "$SITE_VALUES" <(printf 'services:\n  admin:\n    enabled: true\n') ||
    die "R10: check_render rejects the admin render"
  for open_it in loopback=false external=true flags.listen=0.0.0.0:8080 'allowFrom={console}' replicas=2; do
    ! helm template vws charts/vws -f "$SITE_VALUES" --namespace vws --set services.admin.enabled=true \
      --set "services.admin.${open_it}" >/dev/null 2>&1 || die "R10: a loopback admin with ${open_it} was accepted"
  done
  # Release admin with a service portforward would render two Roles named admin-portforward.
  local collide
  collide=$(helm template admin charts/vws -f "$SITE_VALUES" --namespace vws --set services.admin.enabled=true \
    --set services.portforward.port=8080 2>&1) && die "R11: a service Role named like admin-portforward was accepted"
  [[ $collide == *'R11: Role admin-portforward would collide'* ]] || die "R11: the admin-portforward collision failed for another reason: $collide"
  python3 - "$tmp/iam.yaml" <<'PY' || die "R15: Service iam port is not 9095"
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
svc = next(d for d in docs if d.get("kind") == "Service" and d["metadata"]["name"] == "iam")
ports = svc["spec"]["ports"]
ok = (len(ports) == 1 and ports[0].get("name") == "http" and ports[0].get("port") == 9095
      and ports[0].get("targetPort") == "http")
sys.exit(0 if ok else 1)
PY
  console_api_closed "$tmp/default.yaml" || die "R15: the default render publishes an AWS listener or an iam endpoint"
  # vws-iam store (#369): two replicas share the kube store in the release namespace. No volume
  # holds state; the CRDs, a Role on exactly those resources and the API token make it work.
  helm template vws charts/vws -f "$SITE_VALUES" --namespace acme-sys --include-crds \
    --set services.iam.enabled=true > "$tmp/iam-store.yaml"
  python3 - "$tmp/iam-store.yaml" charts/vws/crds <<'PY' || die "R4: vws-iam does not render a shared kube store"
import sys
from pathlib import Path

import yaml

docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
crd_file = Path(sys.argv[2]) / "iam.yaml"
group = "iam." + yaml.safe_load((Path(sys.argv[2]) / "vwsclusters.yaml").read_text())["spec"]["group"]
def one(kind, name, ns="acme-sys"):
    found = [d for d in docs if d.get("kind") == kind and d["metadata"]["name"] == name
             and d["metadata"].get("namespace", ns) == ns]
    return found[0] if len(found) == 1 else None
errors = []
want_crds = sorted(d["metadata"]["name"] for d in yaml.safe_load_all(crd_file.read_text()) if d) if crd_file.exists() else []
plurals = sorted(n.split(".", 1)[0] for n in want_crds)
got_crds = sorted(d["metadata"]["name"] for d in docs if d.get("kind") == "CustomResourceDefinition"
                  and d["spec"]["group"] == group)
if not want_crds or got_crds != want_crds:
    errors.append(f"IAM CRDs rendered {got_crds}, want those of {crd_file}")
if len(plurals) != 17:
    errors.append(f"want the 17 vws-iam kube store resources, got {plurals}")
deploy = one("Deployment", "iam")
if deploy is None or one("StatefulSet", "iam") is not None:
    errors.append("iam must be one Deployment and no StatefulSet")
else:
    spec = deploy["spec"]
    pod = spec["template"]["spec"]
    container = pod["containers"][0]
    if spec.get("replicas") != 2:
        errors.append(f"iam replicas {spec.get('replicas')}, want 2")
    if pod.get("automountServiceAccountToken") is not True:
        errors.append("iam pod must mount its API token")
    if "-namespace=acme-sys" not in (container.get("args") or []):
        errors.append(f"iam args {container.get('args')} lack -namespace=acme-sys")
    volumes = pod.get("volumes") or []
    if volumes != [{"name": "tmp", "emptyDir": {}}] or "volumeClaimTemplates" in spec:
        errors.append(f"iam volumes {volumes}: only the /tmp scratch dir, no state volume")
    if [m["mountPath"] for m in container.get("volumeMounts") or []] != ["/tmp"]:
        errors.append("iam mounts something besides /tmp")
sa = one("ServiceAccount", "iam")
if sa is None or sa.get("automountServiceAccountToken") is not True:
    errors.append("iam ServiceAccount must mount its API token")
if any(d.get("kind") == "PersistentVolumeClaim" for d in docs):
    errors.append("the render has a PersistentVolumeClaim")
role = one("Role", "vws-iam")
want_rules = [{"apiGroups": [group], "resources": plurals, "verbs": ["get", "list", "create", "update", "delete"]}]
if role is None or [dict(r, resources=sorted(r["resources"])) for r in role.get("rules") or []] != want_rules:
    errors.append(f"Role vws-iam in acme-sys has {role and role.get('rules')}, want {want_rules}")
binding = one("RoleBinding", "vws-iam")
if binding is None or binding["roleRef"]["name"] != "vws-iam" or binding.get("subjects") != [
        {"kind": "ServiceAccount", "name": "iam", "namespace": "acme-sys"}]:
    errors.append("RoleBinding vws-iam must bind only ServiceAccount acme-sys/iam")
if one("ClusterRole", "vws-iam", None) is not None:
    errors.append("iam needs no ClusterRole")
for e in errors:
    print("iam store:", e, file=sys.stderr)
sys.exit(1 if errors else 0)
PY

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
  helm template acme charts/vws -f "$SITE_VALUES" --namespace acme -f "$tmp/override.yaml" > "$tmp/override.rendered.yaml"
  python3 scripts/check_render.py "$tmp/override.rendered.yaml" charts/vws/values.yaml acme "$SITE_VALUES" "$tmp/override.yaml"
  local want
  for want in -vm-namespace=acme-vms -image-namespace=acme-images -kube-namespace=acme-vms \
    -kube-nad-namespace=acme-nad -kube-external-subnet=ext -kube-snat=true -kubeconfig=/etc/vws/kube/config -namespace=acme -hypervisor=kubevirt \
    -object-backend=s3 -s3-profile=rgw -s3-endpoint=http://rgw.acme:8080 -s3-bucket-prefix=acme; do
    grep -q -e "- ${want}\$" "$tmp/override.rendered.yaml" || die "R14: rendered args lack ${want}"
  done
  ! grep -q -e 'vws-vms\|vws-images\|vws-nad' "$tmp/override.rendered.yaml" || die "R14: a default namespace name survived a values override"

  grep -q -e 'secretName: acme-network-kubeconfig' "$tmp/override.rendered.yaml" || die "R16: kubeconfigSecret is not mounted"
  # -s3-bucket-prefix is not gated by global.pendingFlags since vws-storage#46 (#105).
  helm template vws charts/vws -f "$SITE_VALUES" --namespace vws --set services.storage.s3.rgw.enabled=true \
    --set services.storage.s3.rgw.endpoint=http://r:8080 --set services.storage.s3.rgw.bucketPrefix=lab |
    grep -q -e '- -s3-bucket-prefix=lab$' || die "R14: bucketPrefix does not render without global.pendingFlags"
  ! grep -q -e '^kind: Secret' "$tmp/default.yaml" "$tmp/override.rendered.yaml" || die "R16: the chart renders a Secret"
  ! grep -q -e 'kubeconfig' "$tmp/default.yaml" || die "R16: the default render mounts or passes a kubeconfig"
  grep -q -e 'name: acme-rgw-s3' "$tmp/override.rendered.yaml" || die "R16: s3.rgw existingSecret is not referenced"
  ! grep -q -e 'VWS_S3_\|object-backend' "$tmp/default.yaml" || die "R16: the default render enables the s3 object backend"
  # bucketPrefix: at most 18 characters, so <prefix>-vws-<40 hex> fits 63 (vws-storage#46, #103).
  printf '%s\n' 'global: {pendingFlags: true}' 'services: {storage: {s3: {rgw: {enabled: true, endpoint: "http://r:8080", bucketPrefix: abcdefghij12345678}}}}' >"$tmp/prefix.yaml"
  helm template vws charts/vws -f "$SITE_VALUES" --namespace vws -f "$tmp/prefix.yaml" | grep -q -e '- -s3-bucket-prefix=abcdefghij12345678$' ||
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
    'services: {storage: {repeatedFlags: {kube-volume-type: []}}}' \
    'services: {storage: {repeatedFlags: {kube-volume-type: gp3=a}}}' \
    'services: {storage: {repeatedFlags: {kube-volume-type: ["gp3=a b"]}}}' \
    'services: {storage: {repeatedFlags: {kube-volume-type: [gp3=a, gp3=a]}}}' \
    'services: {storage: {repeatedFlags: {Kube-X: [a]}}}' \
    'services: {storage: {flags: {kube-volume-type: gp3=a}, repeatedFlags: {kube-volume-type: [gp3=b]}}}' \
    'services: {storage: {s3: {rgw: {enabled: true}}}}' \
    'services: {storage: {s3: {rgw: {enabled: true, endpoint: "http://r:8080", existingSecret: ""}}}}' \
    'services: {storage: {s3: {rgw: {enabled: true, endpoint: "http://r:8080", accessKey: x}}}}' \
    'services: {storage: {s3: {rgw: {enabled: true, endpoint: "http://r:8080", bucketPrefix: abcdefghij123456789}}}}' \
    'services: {storage: {s3: {rgw: {enabled: true, endpoint: "http://r:8080", bucketPrefix: Acme}}}}' \
    'services: {storage: {s3: {rgw: {enabled: true, endpoint: "http://r:8080", bucketPrefix: -acme}}}}' \
    'services: {compute: {healthPort: 80}}' \
    'services: {compute: {healthPort: "8081"}}' \
    'services: {compute: {healthPort: 50051}}' \
    'services: {compute: {healthPort: 8081, healthzPath: /livez}}' \
    'global: {probe: http}'; do
    printf '%s\n' "$bad" > "$tmp/bad.yaml"
    if helm template vws charts/vws -f "$SITE_VALUES" --namespace vws -f "$tmp/bad.yaml" >/dev/null 2>&1; then
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
  local tmp test
  vws_layout_check . || die "R17: scripts/vws layout is broken"
  for test in config region aws-account project; do
    bash "tests/vws/${test}.test.sh" || die "R17: the ${test} tests fail"
  done
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

# upgrade_compat_check: every testdata/upgrade/<version>/ record decodes with the current code, its
# README names the tag and command that wrote it, and its commit carries that chart version
# (R23, vws-deploy#396).
upgrade_compat_die() {
  rm -rf "${upgrade_compat_tmp:-}"
  die "R23: $*"
}

cmd_upgrade_compat() {
  local dir version fixture group commit chart_version
  group=$(sed -n 's/^  group: //p' charts/vws/crds/vwsclusters.yaml)
  [[ -n $group ]] || die "R23: charts/vws/crds/vwsclusters.yaml has no spec.group"
  [[ -d testdata/upgrade ]] || die "R23: testdata/upgrade does not exist"
  [[ -f tests/vws/upgrade-compat.test.sh ]] || die "R23: tests/vws/upgrade-compat.test.sh does not exist"
  upgrade_compat_tmp=$(mktemp -d)
  for dir in testdata/upgrade/*/; do
    [[ -d $dir ]] || upgrade_compat_die "testdata/upgrade has no <version> directory"
    version=$(basename "$dir")
    [[ -f ${dir}README.md ]] || upgrade_compat_die "${dir}README.md does not exist"
    grep -q -E '^Tag: [^[:space:]]' "${dir}README.md" || upgrade_compat_die "${dir}README.md has no 'Tag: <tag>' line"
    grep -q -E '^Command: [^[:space:]]' "${dir}README.md" || upgrade_compat_die "${dir}README.md has no 'Command: <command>' line"
    commit=$(sed -n 's/^Commit: //p' "${dir}README.md")
    [[ $commit =~ ^[0-9a-f]{40}$ ]] || upgrade_compat_die "${dir}README.md has no 'Commit: <full sha>' line"
    git cat-file -e "${commit}^{commit}" 2>/dev/null || upgrade_compat_die "commit ${commit} is not in this repo"
    git merge-base --is-ancestor "$commit" HEAD || upgrade_compat_die "commit ${commit} is not in this history"
    chart_version=$(git show "${commit}:charts/vws/Chart.yaml" | sed -n 's/^version: //p')
    [[ $chart_version == "$version" ]] ||
      upgrade_compat_die "${dir} is version ${version} but charts/vws/Chart.yaml at ${commit:0:7} is ${chart_version}"
    for fixture in "${dir}"*.json; do
      [[ -f $fixture ]] || upgrade_compat_die "testdata/upgrade/${version} holds no .json record"
      jq --arg v "${group}/v1alpha1" '.apiVersion = $v' "$fixture" >"${upgrade_compat_tmp}/cr.json" ||
        upgrade_compat_die "${fixture} is not JSON"
      python3 scripts/vws.d/lib/render.py "${upgrade_compat_tmp}/cr.json" /dev/null >/dev/null ||
        upgrade_compat_die "${fixture} does not decode with the current code"
    done
  done
  rm -rf "$upgrade_compat_tmp"
  echo "upgrade-compat: records decode"
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
# iam.yaml is the vws-iam store (vws-deploy#369): a copy of vws-iam deploy/crds/iam.yaml, group
# iam.<VWSCluster group>. It is optional so a clean copy without it still passes.
iam_path = root / "charts/vws/crds/iam.yaml"
skip = {".git", "build", ".cache", "node_modules"}
files = [p for p in root.rglob("*") if p.is_file() and not skip & set(p.relative_to(root).parts)]
crds = [p for p in files if p.suffix in (".yaml", ".yml") and re.search(r"^kind:\s*CustomResourceDefinition\s*$", p.read_text(errors="ignore"), re.M)]
allowed = [crd_path] + ([iam_path] if iam_path in crds else [])
if sorted(p.relative_to(root) for p in crds) != sorted(p.relative_to(root) for p in allowed):
    sys.exit(f"R18: the VWS CRDs are {crd_path.relative_to(root)} and {iam_path.relative_to(root)}, found {[str(p.relative_to(root)) for p in crds]}")
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

if iam_path in crds:
    for doc in (d for d in yaml.safe_load_all(iam_path.read_text()) if d):
        names = doc["spec"]["names"]
        if doc["spec"].get("group") != f"iam.{group}":
            sys.exit(f"R18: {iam_path.name} CRD {doc['metadata']['name']} group must be iam.{group}")
        if not names["plural"].startswith("iam") or doc["metadata"]["name"] != f"{names['plural']}.iam.{group}":
            sys.exit(f"R18: {iam_path.name} CRD {doc['metadata']['name']} must be iam<kind>s.iam.{group}")
        if doc.get("apiVersion") != "apiextensions.k8s.io/v1" or doc["spec"].get("scope") != "Namespaced":
            sys.exit(f"R18: {iam_path.name} CRDs must be apiextensions.k8s.io/v1 and Namespaced")
        if [v["name"] for v in doc["spec"]["versions"] if v.get("storage")] != ["v1alpha1"]:
            sys.exit(f"R18: {iam_path.name} CRD {doc['metadata']['name']}: v1alpha1 must be the one storage version")
        for version in doc["spec"]["versions"]:
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
    # A bare match only: iam.<group> (the vws-iam API group) is a different group.
    if re.search(r"(?<![\w.-])" + re.escape(group), p.read_text(errors="ignore")):
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
  sed -i "s#^apiVersion: .*#apiVersion: $(sed -n 's/^  group: //p' charts/vws/crds/vwsclusters.yaml)/v1alpha1#" "$tmp/examples/vwscluster/ha.yaml"
  if [[ -f charts/vws/crds/iam.yaml ]]; then
    sed 's/group: iam\./group: other./' charts/vws/crds/iam.yaml > "$tmp/charts/vws/crds/iam.yaml"
    ! crd_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R18: an IAM CRD in another group was accepted"; }
    sed 's/scope: Namespaced/scope: Cluster/' charts/vws/crds/iam.yaml > "$tmp/charts/vws/crds/iam.yaml"
    ! crd_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R18: a cluster-scoped IAM CRD was accepted"; }
    cp charts/vws/crds/iam.yaml "$tmp/charts/vws/crds/"
    crd_check "$tmp" || { rm -rf "$tmp"; die "R18: a clean copy with the IAM CRDs was rejected"; }
  fi
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
  local tmp profile
  tmp=$(mktemp -d)
  profile_keys profiles/*.yaml || die "R19: profiles must set the same keys"
  for profile in single-node small ha; do
    [[ -f profiles/${profile}.yaml ]] || die "R19: profiles/${profile}.yaml is missing"
    helm template vws charts/vws -f "$SITE_VALUES" --namespace vws -f "profiles/${profile}.yaml" > "$tmp/${profile}.yaml"
    python3 scripts/check_render.py "$tmp/${profile}.yaml" charts/vws/values.yaml vws "$SITE_VALUES" "profiles/${profile}.yaml" || die "R19: check_render rejects ${profile}"
    console_api_closed "$tmp/${profile}.yaml" || die "R19: profile ${profile} publishes an AWS listener or an iam endpoint"
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
  # spec.storage.volumeTypes reaches vws-storage as one -kube-volume-type arg per mapped type (#391).
  sed -e 's/    storageClass: fast-rbd/    storageClass: fast-rbd\n    volumeTypes:\n      gp3: {storageClass: fast-rbd, volumeSnapshotClass: snap}\n      sc1: {storageClass: cold, volumeMode: Filesystem}/' \
    examples/vwscluster/small.yaml > "$tmp/vt.cr.in.yaml"
  scripts/vws render "$tmp/vt.cr.in.yaml" "$tmp/vt.values.yaml" >/dev/null || die "R19: vws render fails on a CR with volumeTypes"
  helm template vws charts/vws --namespace vws -f "$tmp/vt.values.yaml" > "$tmp/vt.cr.yaml"
  python3 scripts/check_render.py "$tmp/vt.cr.yaml" charts/vws/values.yaml vws "$tmp/vt.values.yaml" || die "R19: check_render rejects a CR with volumeTypes"
  for want in -kube-volume-type=gp3=fast-rbd//snap -kube-volume-type=sc1=cold/Filesystem; do
    grep -q -e "- ${want}\$" "$tmp/vt.cr.yaml" || die "R19: a CR with volumeTypes does not render ${want}"
  done
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
pods = [d["spec"] for d in docs if d["kind"] == "Pod"] + [d["spec"]["template"]["spec"] for d in docs if d["kind"] in ("Deployment", "Job")]
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
if [d["metadata"]["name"] for d in docs if d["kind"] == "Pod"]:
    fail("the installer runs as the Deployment vws-installer, not a bare Pod")
dep = by.get(("Deployment", "vws-installer"))
if dep is None or dep["spec"].get("replicas") != 1 or (dep["spec"].get("strategy") or {}).get("type") != "Recreate":
    fail("the installer is the Deployment vws-installer with replicas 1 and strategy Recreate (one draft session)")
tpl = dep["spec"]["template"]
if "activeDeadlineSeconds" in tpl["spec"] or tpl["spec"].get("restartPolicy", "Always") != "Always":
    fail("a Deployment pod cannot set activeDeadlineSeconds or restartPolicy; the reaper bounds the rights")
crd = next(d for d in docs if d["kind"] == "CustomResourceDefinition")
want_version = f"{crd['spec']['group']}/{crd['spec']['versions'][0]['name']}"
installer = tpl["spec"]["containers"][0]
want_cmd = ["/opt/vws/bin/vws-installer", f"-api-version={want_version}", "-listen=0.0.0.0:8443",
            "-state-dir=/tmp/vws-installer", "-vws=/opt/vws/scripts/vws",
            "-links=/etc/vws-installer/links/links.json"]
if installer.get("command") != want_cmd or installer.get("args"):
    fail(f"the installer container runs {installer.get('command')} {installer.get('args') or ''}; want {want_cmd} (#191, #205)")
# -links reads links.json from the optional, operator-created ConfigMap vws-installer-links (#205).
links_vol = [v["name"] for v in tpl["spec"].get("volumes", [])
             if v.get("configMap") == {"name": "vws-installer-links", "optional": True}]
links_mount = [m for m in installer.get("volumeMounts", []) if m["name"] in links_vol]
if len(links_mount) != 1 or links_mount[0].get("mountPath") != "/etc/vws-installer/links" \
        or links_mount[0].get("readOnly") is not True or "subPath" in links_mount[0]:
    fail("the installer needs ConfigMap vws-installer-links (optional) mounted read-only at /etc/vws-installer/links, no subPath (#205)")
for kind in ("readinessProbe", "livenessProbe"):
    if ((installer.get(kind) or {}).get("tcpSocket") or {}).get("port") != "api":
        fail(f"the installer container needs a tcpSocket {kind} on port api")
svc = by.get(("Service", "vws-installer"))
if svc is None or svc["spec"].get("type", "ClusterIP") != "ClusterIP":
    fail("the installer Service vws-installer must be type ClusterIP")
for key in ("externalIPs", "loadBalancerIP", "externalName"):
    if key in svc["spec"]:
        fail(f"the installer Service sets {key}")
if any("nodePort" in p for p in svc["spec"]["ports"]):
    fail("the installer Service sets a nodePort")
if not svc["spec"].get("selector") or any(tpl["metadata"]["labels"].get(k) != v for k, v in svc["spec"]["selector"].items()):
    fail("the installer Service selector does not match the installer pod")
names = {p["name"] for c in tpl["spec"]["containers"] for p in c.get("ports", [])}
if any(p["targetPort"] not in names for p in svc["spec"]["ports"]):
    fail("the installer Service targets a port the pod does not name")
if [d["metadata"]["name"] for d in docs if d["kind"] in ("Ingress", "Service") and d["metadata"]["name"] != "vws-installer"]:
    fail("bootstrap.yaml exposes the installer through a second Service or an Ingress")
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
  local tmp ttl gomod goimage
  gomod=$(sed -n 's/^go //p' installer/go.mod)
  goimage=$(sed -n 's/^INSTALLER_GO_IMAGE=//p' scripts/versions.env)
  [[ $goimage == *"/golang:${gomod}-"*"@sha256:"* ]] || die "R20: INSTALLER_GO_IMAGE ${goimage} must be golang:${gomod} (installer/go.mod) pinned by digest"
  grep -q '^FROM ${GO_IMAGE} AS build$' bootstrap/Containerfile || die "R20: bootstrap/Containerfile must build vws-installer from GO_IMAGE"
  grep -q '^COPY --from=build /out/vws-installer /opt/vws/bin/vws-installer$' bootstrap/Containerfile || die "R20: bootstrap/Containerfile must copy vws-installer to /opt/vws/bin"
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
  bad_bootstrap "a NodePort Service" 's/^  type: ClusterIP$/  type: NodePort/'
  bad_bootstrap "two installer replicas" 's/^  replicas: 1$/  replicas: 2/'
  bad_bootstrap "a rolling update" 's/^    type: Recreate$/    type: RollingUpdate/'
  bad_bootstrap "a deadline on the Deployment pod" 's/^      serviceAccountName: vws-installer$/&\n      activeDeadlineSeconds: 60/'
  bad_bootstrap "a Service that selects nothing" '/^  selector:$/{n;s/name: vws-installer/name: other/}'
  bad_bootstrap "a Service on an unnamed port" 's/targetPort: api/targetPort: web/'
  bad_bootstrap "a bare installer Pod" 's/^kind: Deployment$/kind: Pod/'
  bad_bootstrap "an idle installer" 's#^\( *\)- /opt/vws/bin/vws-installer$#\1- sleep\n\1- infinity#'
  bad_bootstrap "a hard-coded API version" 's#-api-version=[^ ]*#-api-version=example.com/v1#'
  bad_bootstrap "an API on another port" 's#-listen=0.0.0.0:8443#-listen=0.0.0.0:9443#'
  bad_bootstrap "no -links" '/- -links=/d'
  bad_bootstrap "-links outside the mount" 's#-links=/etc/vws-installer/links/#-links=/tmp/#'
  bad_bootstrap "no links ConfigMap" 's/name: vws-installer-links$/name: other-links/'
  bad_bootstrap "a required links ConfigMap" 's/^            optional: true$/            optional: false/'
  bad_bootstrap "a writable links mount" '/mountPath: \/etc\/vws-installer\/links$/{n;s/readOnly: true/readOnly: false/}'
  bad_bootstrap "a subPath links mount" 's#^\( *\)mountPath: /etc/vws-installer/links$#&\n\1subPath: links.json#'
  bad_bootstrap "no readiness probe" 's/^          readinessProbe:$/          startupProbe:/'
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

# secrets_check ROOT: no PVE API token or AWS access key literal in any tracked file, and the
# systemd units under packaging/ load secrets only through LoadCredential= (R10, issue #763 AC1).
secrets_check() {
  python3 - "$1" <<'PY'
import re
import shlex
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
tracked = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=True, check=True).stdout
rels = [Path(p) for p in tracked.split("\0") if p]
literals = [("a PVE API token", re.compile("PVEAPIToken" + "=")), ("an AWS access key", re.compile("AKIA" + "[0-9A-Z]{16}"))]
env_line = re.compile(r"^\s*(Environment|EnvironmentFile)\s*=\s*(.*)$")
secret_key = re.compile(r"secret|token|passw|credential|private|api_?key|access_?key", re.I)
runner_reference = re.compile(
    r"pve-token|CREDENTIALS_DIRECTORY|nightly-live|"
    r"(?:secrets|vars|env)\s*(?:\.\s*\w*PVE\w*|\[\s*['\"]\w*PVE\w*['\"]\s*\])|"
    r"\$\{?\w*PVE\w*|\b\w*PVE\w*\s*[:=]", re.I)
failed = False
for rel in rels:
    path = root / rel
    if not path.is_file():
        continue
    text = path.read_text(errors="ignore")
    if rel.parts[0] == ".github" and runner_reference.search(text):
        print(f"R10: runner credential reference in {rel}; keep PVE access on the verification runner", file=sys.stderr)
        failed = True
    for what, pattern in literals:
        if pattern.search(text):
            print(f"R10: {what} literal in {rel}; load it with LoadCredential=", file=sys.stderr)
            failed = True
    unit_kinds = ("service", "target", "socket", "mount", "swap", "timer", "path", "slice", "scope", "automount", "device")
    drop_in = path.suffix == ".conf" and any(part.endswith(tuple(f".{kind}.d" for kind in unit_kinds)) for part in rel.parts[:-1])
    if rel.parts[0] == "packaging" and (path.suffix.lstrip(".") in unit_kinds or drop_in):
        logical = ""
        start = 1
        for number, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith(("#", ";")):
                continue
            if not logical:
                start = number
            logical += line.rstrip()
            if logical.endswith("\\"):
                logical = logical[:-1] + " "
                continue
            match = env_line.match(logical)
            credential = re.match(r"^\s*LoadCredential\s*=\s*(.*)$", logical)
            if credential:
                owner = next((part.removesuffix(".d") for part in rel.parts
                              if part.endswith((".service", ".service.d"))), "")
                for value in shlex.split(credential[1]):
                    forbidden = (
                        re.search(r"aws[-_]", value, re.I) and owner != "vws-aws-proxy.service"
                        or re.search(r"admin[-_]kek", value, re.I) and owner != "vws-console-api.service"
                    )
                    if forbidden:
                        print(f"R10: {rel}:{start} credential belongs to another service", file=sys.stderr)
                        failed = True
            if match and (match[1] == "EnvironmentFile" or any(
                secret_key.search(assignment.partition("=")[0])
                for assignment in shlex.split(match[2]) if "=" in assignment
            )):
                print(f"R10: {rel}:{start} {match[1]}= may carry a secret; use LoadCredential=", file=sys.stderr)
                failed = True
            logical = ""
sys.exit(1 if failed else 0)
PY
}

cmd_secrets() {
  local tmp unit
  secrets_check . || die "R10: secret literals or unit secrets"
  tmp=$(mktemp -d)
  git -C "$tmp" init -q
  mkdir -p "$tmp/.github/workflows"
  printf 'token: ${{ secrets.CONSOLE_TOKEN }}\n' > "$tmp/.github/workflows/x.yml"
  git -C "$tmp" add .github
  secrets_check "$tmp" || { rm -rf "$tmp"; die "R10: a workflow without runner credentials was rejected"; }
  for unit in 'pve-token' 'CREDENTIALS_DIRECTORY' 'nightly-live' 'secrets.PvE_LIVE_TOKEN' \
    "vars['live_pve']" '$PVE_TOKEN' 'PVE_TOKEN: x' 'env.live_pve'; do
    printf '%s\n' "$unit" > "$tmp/.github/workflows/x.yml"
    ! secrets_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R10: a workflow runner credential reference was accepted"; }
  done
  printf 'token: ${{ secrets.CONSOLE_TOKEN }}\n' > "$tmp/.github/workflows/x.yml"
  mkdir -p "$tmp/packaging"
  printf '[Service]\nLoadCredential=pve-token:/etc/vws/pve-token\nEnvironment=VWS_LOG_LEVEL=info\n' > "$tmp/packaging/vws.service"
  git -C "$tmp" add packaging
  secrets_check "$tmp" || { rm -rf "$tmp"; die "R10: a clean unit was rejected"; }
  printf '[Service]\nEnvironment=VWS_MODE=token-free "VWS_PATH=/srv/secret files" '\''VWS_LABEL=private data'\''\n' > "$tmp/packaging/vws.service"
  secrets_check "$tmp" || { rm -rf "$tmp"; die "R10: harmless secret-looking environment values were rejected"; }
  printf '[Service]\nEnvironment=VWS_MODE=dev "DB_PASSWORD=x y"\n' > "$tmp/packaging/vws.service"
  ! secrets_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R10: a later quoted secret assignment was accepted"; }
  printf '[Service]\nEnvironment=VWS_MODE=dev '\''API_KEY=x y'\''\n' > "$tmp/packaging/vws.service"
  ! secrets_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R10: a single-quoted secret assignment was accepted"; }
  printf '[Service]\nEnvironment=VWS_MODE=dev \\ \t\n DB_PASSWORD=x\n' > "$tmp/packaging/vws.service"
  ! secrets_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R10: a continued secret after trailing whitespace was accepted"; }
  printf '[Service]\nEnvironment=PVE_TOKEN=x\n' > "$tmp/packaging/vws.service"
  ! secrets_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R10: a unit with a secret Environment= was accepted"; }
  printf '[Service]\nEnvironment="DB_PASSWORD=x"\n' > "$tmp/packaging/vws.service"
  ! secrets_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R10: a quoted secret Environment= was accepted"; }
  printf '[Service]\nEnvironmentFile=/etc/vws/env\n' > "$tmp/packaging/vws.service"
  ! secrets_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R10: an EnvironmentFile= was accepted"; }
  rm "$tmp/packaging/vws.service"
  for credential in aws-source admin-kek; do
    if [[ $credential == aws-source ]]; then unit=vws-aws-proxy; else unit=vws-console-api; fi
    printf '[Service]\nLoadCredential=%s:/etc/vws/credentials/%s\n' "$credential" "$credential" > "$tmp/packaging/$unit.service"
    git -C "$tmp" add packaging
    secrets_check "$tmp" || { rm -rf "$tmp"; die "R10: credential owner rejected"; }
    mv "$tmp/packaging/$unit.service" "$tmp/packaging/vws-console.service"
    git -C "$tmp" add -A packaging
    ! secrets_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R10: cross-service credential accepted"; }
    printf '[Service]\nLoadCredential=renamed:/etc/vws/credentials/%s\n' "$credential" > "$tmp/packaging/vws-console.service"
    ! secrets_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R10: renamed credential accepted"; }
    rm "$tmp/packaging/vws-console.service"
    mkdir -p "$tmp/packaging/vws-console.service.d"
    printf '[Service]\nLoadCredential=\\\n %s:/etc/vws/credentials/%s\n' "$credential" "$credential" > "$tmp/packaging/vws-console.service.d/override.conf"
    git -C "$tmp" add -A packaging
    ! secrets_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R10: drop-in credential accepted"; }
    rm -r "$tmp/packaging/vws-console.service.d"
    git -C "$tmp" add -A packaging
  done
  for unit in vws.socket vws.mount vws.swap vws.timer vws.path vws.slice vws.scope vws.automount vws.device vws.target vws.service.d/override.conf; do
    mkdir -p "$tmp/packaging/$(dirname "$unit")"
    printf '[Service]\nEnvironment=VWS_LOG_LEVEL=info \\\n# continued directives skip comments\n DB_PASSWORD=x\n' > "$tmp/packaging/$unit"
    git -C "$tmp" add "packaging/$unit"
    ! secrets_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R10: a continued secret Environment= in $unit was accepted"; }
    printf '[Service]\nLoadCredential=db-password:/etc/vws/password\nEnvironment=VWS_LOG_LEVEL=info \\\n VWS_MODE=dev\n' > "$tmp/packaging/$unit"
    secrets_check "$tmp" || { rm -rf "$tmp"; die "R10: clean credentials in $unit were rejected"; }
    printf '[Service]\nEnvironmentFile=\\\n /etc/vws/env\n' > "$tmp/packaging/$unit"
    ! secrets_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R10: a continued EnvironmentFile= in $unit was accepted"; }
    rm "$tmp/packaging/$unit"
  done
  printf 'Authorization: %s\n' "PVEAPIToken""=user@pve!ci=abc" > "$tmp/note.txt"
  secrets_check "$tmp" || { rm -rf "$tmp"; die "R10: an untracked literal was rejected"; }
  git -C "$tmp" add note.txt
  ! secrets_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R10: a PVE API token literal was accepted"; }
  printf 'key %s\n' "AKIA""ABCDEFGHIJKLMNOP" > "$tmp/note.txt"
  ! secrets_check "$tmp" 2>/dev/null || { rm -rf "$tmp"; die "R10: an AWS access key literal was accepted"; }
  rm -rf "$tmp"
}

cmd_english() {
  scripts/guard-agents.sh english .github/english-allowlist || die "R22: the repo is English only; Hangul is allowed only in files that .github/english-allowlist lists with a reason"
}

cmd_observability() {
  # R25 (#50): observability values, the minimal collector and the collector examples.
  local tmp version case name want_tag example
  version=$(sed -n 's/^OTELCOL_VERSION=v\{0,1\}//p' scripts/versions.env)
  [[ -n $version ]] || die "R25: scripts/versions.env has no OTELCOL_VERSION"
  want_tag=$(python3 -c 'import yaml;print(yaml.safe_load(open("charts/vws/values.yaml"))["observability"]["collector"]["image"].split(":")[1].split("@")[0])')
  [[ $want_tag == "$version" ]] || die "R25: observability.collector.image tag ${want_tag} is not OTELCOL_VERSION ${version}"
  command -v otelcol-contrib >/dev/null || die "R25: otelcol-contrib missing; run scripts/install-tools.sh"
  otelcol-contrib --version | grep -q -F " ${version}" || die "R25: otelcol-contrib is not OTELCOL_VERSION ${version}"
  tmp=$(mktemp -d)
  helm template vws charts/vws -f "$SITE_VALUES" --namespace vws > "$tmp/off.yaml"
  if grep -q -E 'name: (OTEL_|SENTRY_|VWS_LOG_LEVEL)|otel-collector' "$tmp/off.yaml"; then
    rm -rf "$tmp"; die "R25: the default render has telemetry env or the collector"
  fi
  cat > "$tmp/collector.yaml" <<'YAML'
observability:
  enabled: true
  defaults:
    resourceAttributes:
      deployment.environment: lab
  collector:
    exporters:
      otlp:
        endpoint: gateway.observability.svc:4317
        authorizationSecret: "42"
services:
  compute:
    observability:
      samplerArg: "1.0"
      protocol: http/protobuf
YAML
  cat > "$tmp/debug.yaml" <<'YAML'
observability:
  enabled: true
  defaults:
    samplerArg: "0.5"
  collector:
    exporters:
      otlp:
        endpoint: http://gateway.observability.svc:4317
        insecure: true
YAML
  cat > "$tmp/external.yaml" <<'YAML'
observability:
  enabled: true
  otlpEndpoint: https://otlp.observability.internal:4317
  otlpHeadersSecret: vws-otlp-headers
  defaults:
    sentry:
      enabled: true
      dsnSecret: "true"
services:
  compute:
    observability:
      logLevel: debug
      sentry:
        enabled: false
YAML
  for case in collector debug external; do
    helm template vws charts/vws -f "$SITE_VALUES" -f "$tmp/${case}.yaml" --namespace vws > "$tmp/${case}.render.yaml"
    python3 scripts/check_render.py "$tmp/${case}.render.yaml" charts/vws/values.yaml vws "$SITE_VALUES" "$tmp/${case}.yaml" ||
      { rm -rf "$tmp"; die "R25: check_render rejects the ${case} observability render"; }
  done
  if grep -q 'otel-collector' "$tmp/external.render.yaml"; then
    rm -rf "$tmp"; die "R25: an external otlpEndpoint still renders the collector"
  fi
  for case in collector debug; do
    python3 - "$tmp/${case}.render.yaml" "$tmp/${case}.otelcol.yaml" <<'PY'
import sys, yaml
cm = next(d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d["kind"] == "ConfigMap" and d["metadata"]["name"] == "otel-collector")
open(sys.argv[2], "w").write(cm["data"]["config.yaml"])
PY
    OTLP_AUTHORIZATION=unused otelcol-contrib validate --config="$tmp/${case}.otelcol.yaml" ||
      { rm -rf "$tmp"; die "R25: otelcol-contrib validate rejects the ${case} collector config"; }
  done
  # Negative: the schema takes only the telemetry keys in defaults and overrides, and no secret value.
  for case in \
    'observability: {defaults: {exporter: otlp}}' \
    'services: {compute: {observability: {otlpEndpoint: "http://x:4317"}}}' \
    'services: {compute: {observability: {sentry: {dsn: "https://k@sentry.internal/1"}}}}' \
    'observability: {defaults: {sentry: {dsn: "https://k@sentry.internal/1"}}}' \
    'observability: {otlpHeaders: "authorization=Bearer x"}' \
    'observability: {collector: {exporters: {otlp: {headers: {authorization: x}}}}}' \
    'observability: {defaults: {samplerArg: "2"}}' \
    'observability: {collector: {image: "otel/opentelemetry-collector-contrib:latest"}}' \
    'observability: {collector: {exporters: {otlp: {endpoint: "http://gw.observability.svc:4317/"}}}}' \
    'observability: {otlpEndpoint: "https://otlp.observability.internal:4317/v1"}'; do
    printf '%s\n' "$case" > "$tmp/bad.yaml"
    if helm template vws charts/vws -f "$SITE_VALUES" -f "$tmp/bad.yaml" --namespace vws > "$tmp/out" 2>&1 ||
      ! grep -q -E 'Additional property|Does not match pattern' "$tmp/out"; then
      rm -rf "$tmp"; die "R25: the values schema did not reject ${case}"
    fi
  done
  # Negative: a render that cannot work fails instead of rendering.
  for case in \
    'observability: {enabled: true, otlpHeadersSecret: vws-otlp-headers}' \
    'observability: {enabled: true, collector: {exporters: {debug: {enabled: false}}}}' \
    'observability: {enabled: true, defaults: {sentry: {enabled: true}}}' \
    'observability: {enabled: true, collector: {exporters: {otlp: {authorizationSecret: vws-otlp-auth}}}}'; do
    printf '%s\n' "$case" > "$tmp/bad.yaml"
    if helm template vws charts/vws -f "$SITE_VALUES" -f "$tmp/bad.yaml" --namespace vws > "$tmp/out" 2>&1 ||
      ! grep -q -E 'observability|sentry' "$tmp/out"; then
      rm -rf "$tmp"; die "R25: helm template did not refuse ${case}"
    fi
  done
  sed 's/limit_percentage: 80/limit_percentage: 180/' "$tmp/debug.otelcol.yaml" > "$tmp/bad.otelcol.yaml"
  if otelcol-contrib validate --config="$tmp/bad.otelcol.yaml" >/dev/null 2>&1; then
    rm -rf "$tmp"; die "R25: otelcol-contrib validate accepted a bad memory_limiter"
  fi
  rm -rf "$tmp"
  # Examples: config only, each with its rights or mounts and its component stability, and valid.
  local examples=docs/observability/collector-examples
  [[ -d $examples ]] || die "R25: ${examples} is missing"
  for name in k8s_cluster k8sobjects kubeletstats hostmetrics filelog; do
    example="${examples}/${name}.yaml"
    [[ -f $example ]] || die "R25: ${example} is missing"
    grep -q '^# Needs: ' "$example" || die "R25: ${example} has no '# Needs:' line"
    grep -q '^# Stability: ' "$example" || die "R25: ${example} has no '# Stability:' line"
    ! grep -q -E '^(apiVersion|kind):' "$example" || die "R25: ${example} is a manifest; examples are collector config only"
    K8S_NODE_NAME=node-0 HOST_ROOT=/ KUBELET_AUTH_TYPE=none otelcol-contrib validate --config="$example" || die "R25: otelcol-contrib validate rejects ${example}"
  done
}

cmd_all() {
  cmd_layout
  cmd_names
  cmd_assets
  cmd_ci
  cmd_cursor
  cmd_agents
  cmd_vws
  cmd_upgrade_compat
  cmd_bootstrap
  cmd_crd
  cmd_bundle
  cmd_hooks
  cmd_docs
  cmd_english
  cmd_secrets
  if [[ -n ${VWS_BASE_REF:-} ]]; then
    cmd_commits "origin/${VWS_BASE_REF}"
  elif git rev-parse --verify origin/main >/dev/null 2>&1; then
    cmd_commits origin/main
  fi
}

usage() {
  cat <<'EOF' >&2
usage: guard.sh <layout|names|assets|ci|cursor|agents|bundle|config|observability|vws|upgrade-compat|bootstrap|crd|profiles|schema|hooks|docs|recovery|placeholders|english|secrets|commits|commit-msg|all>
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
  observability) cmd_observability ;;
  vws) cmd_vws ;;
  upgrade-compat) cmd_upgrade_compat ;;
  bootstrap) cmd_bootstrap ;;
  crd) cmd_crd ;;
  profiles) cmd_profiles ;;
  schema) cmd_schema ;;
  hooks) cmd_hooks ;;
  docs) cmd_docs ;;
  recovery) cmd_recovery ;;
  placeholders) cmd_placeholders "$@" ;;
  english) cmd_english ;;
  secrets) cmd_secrets ;;
  commits) cmd_commits "$@" ;;
  commit-msg) cmd_commit_msg "$@" ;;
  all) cmd_all ;;
  *) usage; exit 2 ;;
esac
