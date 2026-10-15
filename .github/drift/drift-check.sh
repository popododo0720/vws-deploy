#!/usr/bin/env bash
# Guardrail drift check across the vws-* repos (vws-deploy#300). See .github/drift/drift_check.py.
# Usage: .github/drift/drift-check.sh [--owner OWNER] [--repos FILE] [--markdown OUT.md]
set -euo pipefail
exec python3 "$(dirname "$0")/drift_check.py" "$@"
