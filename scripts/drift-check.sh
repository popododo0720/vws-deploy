#!/usr/bin/env bash
# Guardrail drift check across the vws-* repos (vws-deploy#300). See scripts/drift/drift_check.py.
# Usage: scripts/drift-check.sh [--owner OWNER] [--repos FILE] [--markdown OUT.md]
set -euo pipefail
exec python3 "$(dirname "$0")/drift/drift_check.py" "$@"
