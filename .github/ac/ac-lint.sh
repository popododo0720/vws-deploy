#!/usr/bin/env bash
# List an issue's acceptance-criteria boxes and flag any ticked box without evidence. Read-only.
# Ported from the box vws-issue-ac skill (vws-deploy#399); keep the two copies in step.
set -euo pipefail
usage() {
  cat <<'U'
Usage: ac-lint.sh --repo OWNER/REPO --issue N     (reads the live body with gh)
       ac-lint.sh --file BODY.md                   (a draft body)
Prints every "- [ ]" / "- [x]" line as OPEN, OK or NO-EVIDENCE. Evidence is a URL, an absolute
path, a file:line, a #N or owner/repo#N reference, or a Test name on the box line or the lines
indented under it. Exits 1 if any ticked box lacks evidence. OPEN boxes are follow-up candidates.
U
}
repo="" issue="" file=""
while (($#)); do
  case $1 in
    --repo) repo=$2; shift 2 ;; --issue) issue=$2; shift 2 ;; --file) file=$2; shift 2 ;;
    -h | --help) usage; exit 0 ;; *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [[ -n $file ]]; then body=$(cat "$file")
elif [[ $repo == */* && $issue =~ ^[0-9]+$ ]]; then body=$(gh issue view "$issue" -R "$repo" --json body -q .body)
else usage >&2; exit 2; fi
tr -d '\r' <<<"$body" | awk '
  function flush() {
    if (box == "") return
    ev = (blk ~ /https?:\/\/|(^|[[:space:]`(])\/[[:alnum:]._-]+\/|[[:alnum:]_.-]+:[0-9]+|#[0-9]+|Test[[:alnum:]_]+/)
    if (box == "x") { st = ev ? "OK" : "NO-EVIDENCE"; if (!ev) bad++ } else st = "OPEN"
    printf "%-11s %s\n", st, substr(title, 1, 110); n++; box = ""
  }
  /^[[:space:]]*[-*] \[[ xX]\] / { flush(); box = ($0 ~ /\[[xX]\]/) ? "x" : " "; title = $0; sub(/^[[:space:]]*[-*] /, "", title); blk = $0; next }
  box != "" && /^[[:space:]]+[^[:space:]]/ { blk = blk " " $0; next }
  { flush() }
  END { flush(); printf "%d boxes, %d ticked without evidence\n", n, bad; exit bad > 0 }'
