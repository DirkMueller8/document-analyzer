r"""Sync a (password-protected) .odt document into data/corpus.db and print a validation report.

    python -m ingest.run "C:\path\to\notes.odt"
    python -m ingest.run "C:\path\to\notes.odt" --dry-run --show-snippets

If the file is encrypted, you are asked for the password. It is never stored, and the
file is decrypted in memory only. Run it as often as you like: only new or edited
entries are added, everything else is kept.

The report shows only positions and counts unless --show-snippets is given, so it is
safe to paste anywhere. Fix problems via data/overrides.yaml (keys are the "pos" numbers):

    dates:
      1234: "Dez 1989"
    skip: [17, 18]
"""
from __future__ import annotations

import argparse
import getpass
import sys
from datetime import date
from pathlib import Path

from analysis.measurements import count_ignored_variants, count_skipped_lines, extract, load_kinds, store_all

from .odf_crypto import WrongPassword, decrypt_odt, is_encrypted
from .odt_parser import extract_blocks
from .segment import Overrides, build_report, segment
from .store import connect, sync_entries

DATA = Path(__file__).resolve().parents[2] / "data"


def open_document(path: Path, password: str | None = None, attempts: int = 3):
    """Return something extract_blocks can read; prompts for the password if needed."""
    if not is_encrypted(path):
        return path
    for _ in range(attempts):
        pw = password if password is not None else getpass.getpass("Document password: ")
        try:
            return decrypt_odt(path, pw)
        except WrongPassword:
            if password is not None:
                raise
            print("Wrong password.", file=sys.stderr)
        finally:
            pw = None
    raise SystemExit("Giving up after 3 wrong passwords.")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("odt", type=Path)
    ap.add_argument("--db", type=Path, default=DATA / "corpus.db")
    ap.add_argument("--overrides", type=Path, default=DATA / "overrides.yaml")
    ap.add_argument("--show-snippets", action="store_true", help="include text excerpts (private!)")
    ap.add_argument("--dry-run", action="store_true", help="report only, do not write the database")
    ap.add_argument("--full-report", action="store_true", help="list every position, not just the first 20")
    ap.add_argument("--no-index", action="store_true", help="skip updating the search index")
    args = ap.parse_args(argv)

    if not args.dry_run and args.db.parent == DATA and not (DATA / ".vault").exists():
        raise SystemExit(f"{args.db.parent} not found. Is the vault open? (scripts\\vault-open.ps1)")

    blocks = extract_blocks(open_document(args.odt))
    entries = segment(blocks, Overrides.load(args.overrides))
    report = build_report(blocks, entries)

    snippets = {b.pos: b.text[:70] for b in blocks} if args.show_snippets else None
    print(report.render(snippets, limit=None if args.full_report else 20))

    kinds = load_kinds()
    found = [m for e in entries if e.date_start and e.precision == "day"
             for m in extract(e.text, e.date_start, kinds)]
    print("\nMeasurements found: " + (", ".join(
        f"{k.name}={len(ms)} ({min(m.date for m in ms)} .. {max(m.date for m in ms)})"
        if (ms := [m for m in found if m.kind == k.name]) else f"{k.name}=0" for k in kinds) or "none configured"))
    for label, counter in (("Ignored variants (similar to the format, but not exactly)", count_ignored_variants),
                           ("Excluded lines (skip_lines, e.g. references to other days)", count_skipped_lines)):
        totals: dict[str, int] = {}
        for e in entries:
            if e.date_start and e.precision == "day":
                for name, n in counter(e.text, kinds).items():
                    totals[name] = totals.get(name, 0) + n
        if totals:
            print(f"{label}: " + ", ".join(f"{k}={n}" for k, n in sorted(totals.items())))

    if not args.dry_run:
        # empty date lines and pre-written future dates carry no content: store them once filled in
        today = date.today()
        storable = [e for e in entries if e.text.strip() and not (e.date_start and e.date_start > today)]
        con = connect(args.db)
        stats = sync_entries(con, storable)
        print(f"\nSynced {args.db}: {stats} ({len(entries) - len(storable)} empty/future entries skipped)")
        print(f"Stored {store_all(con, kinds)} measurements")
        if not args.no_index:
            update_search_index(con)


def update_search_index(con) -> None:
    from search.index import update_index
    from search.ollama_client import EMBED_MODEL, OllamaError, embed, require

    try:
        require(EMBED_MODEL)
        n = update_index(con, lambda texts: embed(texts, EMBED_MODEL), EMBED_MODEL)
        print(f"Search index: {n} new chunks ({EMBED_MODEL})")
    except OllamaError as e:
        print(f"Search index not updated: {e}")


if __name__ == "__main__":
    main()
