#!/usr/bin/env bash
# Repo checks for the rules in AGENTS.md.
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
  if find . -path ./.git -prune -o -path ./packaging -prune -o \( -name '*.service' -o -name '*.target' \) -print | grep -q .; then
    die "R10: systemd units live only under packaging/"
  fi
  [[ ! -e host ]] || die "R10: host/ must not exist"
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
    "$@"
}

cmd_ci() {
  if r8_scan .github Makefile scripts; then
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
  printf '%s\n' 'ci: update one guard' > "$tmp"
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
for path in (Path("scripts/guard.sh"), Path(".githooks/pre-push")):
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

cmd_cursor() {
  [[ ! -e .cursor ]] || die "R9: .cursor/ does not belong in this repo"
  if grep -R -n -i -E 'api\.cursor\.com|cursor-agent|cursor cloud|CloudAgent' .github Makefile scripts 2>/dev/null \
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

# bootstrap_check FILE TTL: the bootstrap template keeps the installer rules (R20).
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
want_version = "VWS_API_GROUP/VWS_CRD_VERSION"
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
  ttl=$(sed -n 's/^INSTALLER_TTL_SECONDS=//p' scripts/versions.env)
  tmp=$(mktemp -d)
  # Keep the retired API placeholders in the template; only check its security structure.
  local image
  image=$(sed -n 's/^VWS_INSTALLER_IMAGE=//p' scripts/versions.env)
  sed -e "s#INSTALLER_IMAGE#${image}#g" \
    -e "s#REAPER_DEADLINE_SECONDS#$((ttl + 600))#g" \
    -e "s#INSTALLER_TTL_SECONDS#${ttl}#g" bootstrap/bootstrap.yaml > "$tmp/bootstrap.yaml"
  bootstrap_check "$tmp/bootstrap.yaml" "$ttl" || die "R20: bootstrap rules"
  bad_bootstrap() { # NAME SED
    sed -e "$2" "$tmp/bootstrap.yaml" > "$tmp/b.yaml"
    cmp -s "$tmp/bootstrap.yaml" "$tmp/b.yaml" && { rm -rf "$tmp"; die "R20: negative test '$1' changed nothing"; }
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
  local version name example
  version=$(sed -n 's/^OTELCOL_VERSION=v\{0,1\}//p' scripts/versions.env)
  [[ -n $version ]] || die "R25: scripts/versions.env has no OTELCOL_VERSION"
  command -v otelcol-contrib >/dev/null || die "R25: otelcol-contrib missing; run scripts/install-tools.sh"
  otelcol-contrib --version | grep -q -F " ${version}" || die "R25: otelcol-contrib is not OTELCOL_VERSION ${version}"
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
  cmd_assets
  cmd_ci
  cmd_cursor
  cmd_agents
  cmd_vws
  cmd_bootstrap
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
usage: guard.sh <layout|assets|ci|cursor|agents|bundle|observability|vws|bootstrap|hooks|docs|english|secrets|commits|commit-msg|all>
EOF
}

command_name=${1:-}
shift || true
case "$command_name" in
  layout) cmd_layout ;;
  assets) cmd_assets ;;
  ci) cmd_ci ;;
  cursor) cmd_cursor ;;
  agents) cmd_agents ;;
  bundle) cmd_bundle ;;
  observability) cmd_observability ;;
  vws) cmd_vws ;;
  bootstrap) cmd_bootstrap ;;
  hooks) cmd_hooks ;;
  docs) cmd_docs ;;
  english) cmd_english ;;
  secrets) cmd_secrets ;;
  commits) cmd_commits "$@" ;;
  commit-msg) cmd_commit_msg "$@" ;;
  all) cmd_all ;;
  *) usage; exit 2 ;;
esac
