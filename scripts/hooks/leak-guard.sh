#!/bin/sh
# Shared checks for pre-commit / pre-push (sourced). Blocks private files and any term
# listed in private/denylist.txt (one term per line, case-insensitive, # = comment).
ROOT=$(git rev-parse --show-toplevel)
DENY="$ROOT/private/denylist.txt"

# stdin: file paths -> prints the forbidden ones
blocked_paths() {
  grep -E '(^|/)(data|private)/|\.db$|\.db-|\.vhdx?$|(^|/)\.env$|\.odt$' \
    | grep -v -E '^samples/sample_entries[a-z0-9_]*\.odt$'
}

# stdin: text, $1: label for the message. Returns 1 on a hit.
# Done in Python: Git for Windows' grep crashes on case-insensitive term lists with umlauts.
check_text() {
  [ -f "${LEAK_GUARD_DENYLIST:-$DENY}" ] || return 0
  for py in python3 python py; do
    if command -v "$py" >/dev/null 2>&1; then
      "$py" "$ROOT/scripts/hooks/leak_guard.py" "$1"
      return $?
    fi
  done
  echo "leak-guard: no Python found - refusing (fail closed)." >&2
  return 1
}
