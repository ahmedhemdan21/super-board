#!/usr/bin/env bash
# Entry point for the paid skill evals: puts the offline stubs first on PATH and runs
# `claude plugin eval` from the pack root. Extra arguments pass through (--case, --runs …).
# On Windows it stops at once with exit 75 (evals/_stubs/platform-guard.sh): run in WSL or CI.
#
#   bash evals/run.sh [--case 'collect-*'] [--runs 1 --keep-temp]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/_stubs/platform-guard.sh"
cd "$HERE/.."
command -v claude >/dev/null || { echo "evals: the claude CLI is not on PATH" >&2; exit 127; }
PATH="$HERE/_stubs/bin:$PATH" exec claude plugin eval . \
  --scaffold --allow-tools Bash Write Edit Agent --ablation none --runs 3 --max-cost-usd 5 "$@"
