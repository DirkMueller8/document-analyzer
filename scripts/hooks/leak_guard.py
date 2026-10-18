"""Denylist check for the leak-guard git hooks.

Reads text on stdin and the denylist (default: private/denylist.txt, one term per line,
# = comment). Exits 1 and lists the hits if any term occurs. Matching is case-insensitive
and Unicode-aware; terms of up to 3 characters must match a whole word (so "ana" does
not hit "banana"), longer terms match anywhere (so "Lena" also catches "Lenas").
Prefix a term with "=" to force whole-word matching (e.g. "=Max" ignores "maximum").
"""
import os
import re
import sys
from pathlib import Path

SHORT = 3


def load_terms(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    return [t.strip() for t in lines if t.strip() and not t.strip().startswith("#")]


def find_hits(text: str, terms: list[str]) -> list[str]:
    hits = set()
    for term in terms:
        whole = term.startswith("=") or len(term) <= SHORT
        term = term.lstrip("=")
        if not term:
            continue
        pattern = re.escape(term)
        if whole:
            pattern = rf"(?<!\w){pattern}(?!\w)"
        for m in re.finditer(pattern, text, re.IGNORECASE):
            hits.add(m.group(0))
    return sorted(hits, key=str.casefold)


def main() -> int:
    label = sys.argv[1] if len(sys.argv) > 1 else "text"
    root = Path(__file__).resolve().parents[2]
    deny = Path(os.environ.get("LEAK_GUARD_DENYLIST", root / "private" / "denylist.txt"))
    text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    hits = find_hits(text, load_terms(deny))
    if hits:
        print(f"leak-guard: {label} contains denylisted term(s):", file=sys.stderr)
        for h in hits:
            print(f"  {h}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
