#!/usr/bin/env bash
# Admin audit of ruleset bypass actors in every vws-* repo (vws-deploy#458).
# Run it by hand on the VM with the caller's own gh auth: the dsh fine-grained PAT, which has
# Administration: Read-only. Never run it in Actions, and never copy the credential into Actions
# secrets, into a file in the repo or into a log. The script reads only the gh login name; it never
# reads or prints a token. Every call is a read-only GET.
# GitHub shows bypass_actors only to a caller with admin read, so with any other token every repo fails
# as "unverified"; it never passes. Exit status: 0 when no repo has a bypass actor, 3 when one does or
# the list is unreadable, 2 on a usage error.
# Usage: .github/drift/audit-bypass-actors.sh [--owner OWNER] [--repos FILE]
set -euo pipefail
echo "bypass audit $(date -u +%Y-%m-%dT%H:%M:%SZ) at $(git -C "$(dirname "$0")" rev-parse --short HEAD 2>/dev/null || echo unknown) as gh account $(gh api user --jq .login 2>/dev/null || echo unknown)"
exec python3 "$(dirname "$0")/drift_check.py" --bypass-audit "$@"
