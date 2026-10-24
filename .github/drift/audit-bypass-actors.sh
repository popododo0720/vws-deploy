#!/usr/bin/env bash
# Admin audit of ruleset bypass actors in every vws-* repo (vws-deploy#458).
# Box only: run it from the operator box with that box's own admin gh login. Never run it in Actions,
# and never copy the admin credential to the VM, into Actions secrets, into a file or into a log.
# GitHub shows bypass_actors only to an admin caller, so with any other token every repo fails as
# "unverified"; it never passes. Exit status: 0 when no repo has a bypass actor, 3 when one does or
# the list is unreadable, 2 on a usage error.
# Usage: .github/drift/audit-bypass-actors.sh [--owner OWNER] [--repos FILE]
set -euo pipefail
exec python3 "$(dirname "$0")/drift_check.py" --bypass-audit "$@"
