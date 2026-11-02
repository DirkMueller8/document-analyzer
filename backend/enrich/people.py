"""Review the people index: list, find likely variants, merge, rename, ignore.

    python -m enrich.people list [--min 2]      # names with mentions, first and last date
    python -m enrich.people suggest             # likely variants of the same person
    python -m enrich.people merge "Lena" "Lena B." "Lenchen"   # first name is kept, the others become aliases
    python -m enrich.people rename "Lena" "Lena Berg"          # the old name stays as an alias
    python -m enrich.people ignore "Name"       # never a person (club, place, ...): removed now and in future runs
    python -m enrich.people unignore "Name"     # entries containing the name are extracted again on the next run

Aliases make the change stick: later extraction runs map every variant to the same person.
The output contains names from your diary - for your screen only.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from ingest.store import connect

DATA = Path(__file__).resolve().parents[2] / "data"
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


class PeopleError(ValueError):
    pass


@dataclass(frozen=True)
class Person:
    id: int
    name: str
    aliases: tuple[str, ...]
    mentions: int
    first: str | None
    last: str | None


def people(con: sqlite3.Connection, min_mentions: int = 0) -> list[Person]:
    rows = con.execute(
        "SELECT p.id, p.canonical_name, p.aliases, COUNT(e.id), MIN(e.date_start), MAX(e.date_start)"
        " FROM people p LEFT JOIN mentions m ON m.person_id = p.id LEFT JOIN entries e ON e.id = m.entry_id"
        " GROUP BY p.id HAVING COUNT(e.id) >= ? ORDER BY COUNT(e.id) DESC, p.canonical_name", (min_mentions,))
    return [Person(i, n, tuple(json.loads(a or "[]")), c, f, l) for i, n, a, c, f, l in rows]


def find(con: sqlite3.Connection, name: str) -> int:
    key = name.strip().casefold()
    for id_, canonical, aliases in con.execute("SELECT id, canonical_name, aliases FROM people"):
        if key in {canonical.casefold(), *(a.casefold() for a in json.loads(aliases or "[]"))}:
            return id_
    raise PeopleError(f"No person named {name!r} (see: list).")


def _person(con: sqlite3.Connection, id_: int) -> tuple[str, list[str]]:
    name, aliases = con.execute("SELECT canonical_name, aliases FROM people WHERE id = ?", (id_,)).fetchone()
    return name, json.loads(aliases or "[]")


def _set_aliases(con: sqlite3.Connection, id_: int, aliases: list[str], canonical: str) -> None:
    seen = {canonical.casefold()}
    clean = []
    for a in aliases:
        if a.casefold() not in seen:
            seen.add(a.casefold())
            clean.append(a)
    con.execute("UPDATE people SET aliases = ? WHERE id = ?", (json.dumps(clean, ensure_ascii=False), id_))


def merge(con: sqlite3.Connection, keep: str, others: list[str]) -> int:
    """Merge `others` into `keep`. Returns the number of mentions moved."""
    target = find(con, keep)
    ids = list(dict.fromkeys(find(con, o) for o in others))
    if target in ids:
        raise PeopleError("A name cannot be merged into itself.")
    name, aliases = _person(con, target)
    moved = 0
    with con:
        for id_ in ids:
            other_name, other_aliases = _person(con, id_)
            aliases += [other_name, *other_aliases]
            moved += con.execute("INSERT OR IGNORE INTO mentions (person_id, entry_id)"
                                 " SELECT ?, entry_id FROM mentions WHERE person_id = ?", (target, id_)).rowcount
            con.execute("DELETE FROM people WHERE id = ?", (id_,))
        _set_aliases(con, target, aliases, name)
    return moved


def rename(con: sqlite3.Connection, old: str, new: str) -> None:
    id_ = find(con, old)
    try:
        other = find(con, new)
    except PeopleError:
        other = None
    if other is not None and other != id_:
        raise PeopleError(f"{new!r} is another person already; use merge.")
    name, aliases = _person(con, id_)
    with con:
        con.execute("UPDATE people SET canonical_name = ? WHERE id = ?", (new, id_))
        _set_aliases(con, id_, [a for a in [*aliases, name] if a.casefold() != new.casefold()], new)


def ignore(con: sqlite3.Connection, name: str) -> int:
    """Put a name (and, if it is a person, all its aliases) on the ignore list. Returns removed mentions."""
    try:
        id_ = find(con, name)
    except PeopleError:
        id_ = None
    keys = {name.strip().casefold()}
    removed = 0
    with con:
        if id_ is not None:
            canonical, aliases = _person(con, id_)
            keys |= {canonical.casefold(), *(a.casefold() for a in aliases)}
            removed = con.execute("SELECT COUNT(*) FROM mentions WHERE person_id = ?", (id_,)).fetchone()[0]
            con.execute("DELETE FROM people WHERE id = ?", (id_,))
        con.executemany("INSERT OR IGNORE INTO people_ignore (name_key) VALUES (?)", [(k,) for k in keys])
    return removed


def unignore(con: sqlite3.Connection, name: str) -> int:
    """Take a name off the ignore list; entries whose text contains it are extracted again next run."""
    key = name.strip().casefold()
    with con:
        if not con.execute("DELETE FROM people_ignore WHERE name_key = ?", (key,)).rowcount:
            raise PeopleError(f"{name!r} is not on the ignore list.")
        pattern = re.compile(r"(?<!\w)" + re.escape(name.strip()), re.IGNORECASE)
        ids = [i for i, text in con.execute("SELECT id, text FROM entries") if pattern.search(text)]
        con.executemany("DELETE FROM entry_meta WHERE entry_id = ?", [(i,) for i in ids])
    return len(ids)


def _fold(name: str) -> str:
    s = name.casefold().translate(_UMLAUTS)
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return re.sub(r"[^\w ]", "", s).strip()


def _one_edit(a: str, b: str) -> bool:
    """True if a and b differ by exactly one inserted, deleted or replaced character."""
    if abs(len(a) - len(b)) > 1 or a == b:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i + (len(a) == len(b)):] == b[i + 1:]


def suggest(con: sqlite3.Connection) -> list[tuple[Person, Person, str]]:
    """Pairs that are probably the same person, with the reason."""
    ps = people(con)
    out = []
    for i, a in enumerate(ps):
        fa = _fold(a.name)
        for b in ps[i + 1:]:
            fb = _fold(b.name)
            if fa == fb:
                reason = "same spelling apart from case, accents or umlauts"
            elif fa.split()[:1] == fb.split()[:1] and (" " in fa or " " in fb):
                reason = "same first name, one with more"
            elif min(len(fa), len(fb)) >= 4 and _one_edit(fa, fb):
                reason = "one letter different"
            else:
                continue
            out.append((a, b, reason))
    return out


def _fmt(p: Person) -> str:
    span = f"{p.first or '?'} .. {p.last or '?'}" if p.mentions else "no mentions"
    alias = f"  (also: {', '.join(p.aliases)})" if p.aliases else ""
    return f"{p.name}: {p.mentions} entries, {span}{alias}"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DATA / "corpus.db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--min", type=int, default=0, help="only people with at least this many entries")
    sub.add_parser("suggest")
    sub.add_parser("ignored", help="show the ignore list")
    m = sub.add_parser("merge")
    m.add_argument("keep")
    m.add_argument("others", nargs="+")
    r = sub.add_parser("rename")
    r.add_argument("old")
    r.add_argument("new")
    for cmd in ("ignore", "unignore"):
        sub.add_parser(cmd).add_argument("names", nargs="+")
    args = ap.parse_args(argv)
    if not args.db.exists():
        raise SystemExit(f"{args.db} not found. Open the vault and run the import first.")
    con = connect(args.db)
    print("Names from your diary - for this screen only.\n", file=sys.stderr)
    try:
        if args.cmd == "list":
            ps = people(con, args.min)
            print("\n".join(_fmt(p) for p in ps) or "No people yet.")
            print(f"\n{len(ps)} people", file=sys.stderr)
        elif args.cmd == "suggest":
            pairs = suggest(con)
            for a, b, reason in pairs:
                print(f"{a.name}  <->  {b.name}   ({reason}; {a.mentions} / {b.mentions} entries)")
            print(f"\n{len(pairs)} suggestions. Merge with: .\\people.ps1 merge \"<keep>\" \"<other>\"",
                  file=sys.stderr)
        elif args.cmd == "ignored":
            print("\n".join(r[0] for r in con.execute("SELECT name_key FROM people_ignore ORDER BY 1")) or "Empty.")
        elif args.cmd == "merge":
            print(f"Merged; {merge(con, args.keep, args.others)} mentions moved.")
        elif args.cmd == "rename":
            rename(con, args.old, args.new)
            print("Renamed; the old name is kept as an alias.")
        elif args.cmd == "ignore":
            for n in args.names:
                print(f"Ignored; {ignore(con, n)} mentions removed.")
        elif args.cmd == "unignore":
            for n in args.names:
                print(f"Taken off the ignore list; {unignore(con, n)} entries will be extracted again "
                      "on the next .\\enrich.ps1 run.")
    except PeopleError as e:
        raise SystemExit(str(e))


if __name__ == "__main__":
    main()
