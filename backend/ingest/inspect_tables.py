"""Show the structure of every table in an .odt - WITHOUT its content (safe to share).

    python -m ingest.inspect_tables "C:\\path\\to\\notes.odt"

Per table: position range, rows, cells per row, how many rows got a date and how,
and the "shape" of the first undated rows (digits -> 9, letters -> a, month and
weekday names -> <M>/<W>, long text -> <text N chars>).
"""
from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict
from pathlib import Path

from .dates import MONTHS, WEEKDAYS
from .odt_parser import Block, extract_blocks
from .run import open_document
from .segment import block_date

NAMES = {**{m: "<M>" for m in MONTHS}, **{w: "<W>" for w in WEEKDAYS}}


def shape(text: str, max_len: int = 25) -> str:
    """Content-free outline of a short cell; long cells only report their length."""
    if len(text) > max_len:
        return f"<text {len(text)} chars>"
    def word(m: re.Match) -> str:
        w = m.group(0)
        return NAMES.get(w.lower(), "a" * len(w))
    s = re.sub(r"[^\W\d_]+", word, text)
    return re.sub(r"\d", "9", s).replace("\n", "⏎").replace("\t", " | ")


def inspect(blocks: list[Block], samples: int = 5) -> str:
    tables: dict[int, list[Block]] = defaultdict(list)
    for b in blocks:
        if b.table is not None:
            tables[b.table].append(b)
    if not tables:
        return "No tables found."

    out = [f"Tables: {len(tables)}"]
    for t, rows in tables.items():
        found = [(b, *block_date(b)) for b in rows]
        how = Counter(method for _, _, method in found)
        dates = [info.start for _, info, _ in found if info]
        cells = Counter(len(b.cells) for b in rows)
        dv = sum(any(b.date_values) for b in rows)
        out += [
            "",
            f"Table {t}: pos {rows[0].pos}-{rows[-1].pos}, {len(rows)} blocks",
            f"  cells per block: " + ", ".join(f"{k} cell(s) x{v}" for k, v in sorted(cells.items())),
            f"  date-formatted cells: {dv}",
            f"  dated: {len(dates)}" + (f" ({min(dates)} .. {max(dates)})" if dates else ""),
            f"  found via: " + ", ".join(f"{k}={v}" for k, v in how.most_common()),
        ]
        # short undated blocks are the interesting ones (missed dates); long ones are text
        undated = [b for b, info, _ in found if not info and len(b.text) <= 40][:samples]
        if undated and len(dates) < len(rows):
            out.append(f"  first short undated blocks (shape only):")
            out += [f"    pos {b.pos}: " + " | ".join(shape(c) for c in b.cells) for b in undated]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("odt", type=Path)
    args = ap.parse_args(argv)
    print(inspect(extract_blocks(open_document(args.odt))))


if __name__ == "__main__":
    main()
