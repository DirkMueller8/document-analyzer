#!/bin/sh
# Shared checks for pre-commit / pre-push (sourced). Blocks private files and any term
# listed in the denylist named by the LEAK_GUARD_DENYLIST user environment variable
# (outside this repository). Without a denylist, nothing is pushed (fail closed).
ROOT=$(git rev-parse --show-toplevel)

# stdin: file paths -> prints the forbidden ones
blocked_paths() {
  grep -E '(^|/)(data|private)/|\.db$|\.db-|\.vhdx?$|(^|/)\.env$|\.odt$' \
    | grep -v -E '^samples/sample_entries[a-z0-9_]*\.odt$'
}

# stdin: text, $1: label for the message. Returns 1 on a hit.
# Done in Python: Git for Windows' grep crashes on case-insensitive term lists with umlauts.
check_text() {
  if [ -z "$LEAK_GUARD_DENYLIST" ] || [ ! -f "$LEAK_GUARD_DENYLIST" ]; then
    echo "leak-guard: LEAK_GUARD_DENYLIST is not set or the file is missing - refusing (fail closed)." >&2
    echo "            Set it once: [Environment]::SetEnvironmentVariable(\"LEAK_GUARD_DENYLIST\", \"<path>\", \"User\")" >&2
    return 1
  fi
  for py in python3 python py; do
    if command -v "$py" >/dev/null 2>&1; then
      "$py" "$ROOT/scripts/hooks/leak_guard.py" "$1"
      return $?
    fi
  done
  echo "leak-guard: no Python found - refusing (fail closed)." >&2
  return 1
}
