"""Group text blocks into dated entries and build a validation report."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from .dates import DateInfo, parse_leading_date
from .odt_parser import Block


@dataclass
class Entry:
    date_start: date | None
    date_end: date | None
    precision: str | None
    raw_date: str | None
    source_pos: int
    format: str
    paragraphs: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.paragraphs)


@dataclass
class Overrides:
    """Manual fixes from overrides.yaml, keyed by block position (see report)."""
    dates: dict[int, str] = field(default_factory=dict)  # pos -> "12.03.1995" / "Dez 1989"
    skip: set[int] = field(default_factory=set)

    @classmethod
    def load(cls, path: str | Path | None) -> "Overrides":
        if not path or not Path(path).exists():
            return cls()
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(
            dates={int(k): str(v) for k, v in (data.get("dates") or {}).items()},
            skip={int(p) for p in data.get("skip") or []},
        )


def _from_date_value(value: str, raw: str, rest: str) -> DateInfo | None:
    try:
        d = date.fromisoformat(value[:10])
    except ValueError:
        return None
    return DateInfo(d, d, "day", raw or d.strftime("%d.%m.%Y"), rest)


def block_date(b: Block) -> tuple[DateInfo | None, str]:
    """Date a block starts with, and how it was found (for the table inspector).

    Plain paragraphs: leading date. Table rows additionally: a date-formatted cell
    (LibreOffice's stored date value), or a date at the start of one of the first 3 cells.
    """
    if info := parse_leading_date(b.text):
        return info, "leading"
    if b.kind != "table" or len(b.cells) < 2:
        return None, "none"

    def others(i: int, first: str = "") -> str:
        return "\t".join(t for t in [first, *(c for j, c in enumerate(b.cells) if j != i)] if t)

    for i, dv in enumerate(b.date_values[:3]):
        if dv and (info := _from_date_value(dv, b.cells[i], others(i))):
            return info, f"date-value col {i + 1}"
    for i, cell in enumerate(b.cells[:3]):
        if info := parse_leading_date(cell):
            return DateInfo(info.start, info.end, info.precision, info.raw, others(i, info.rest)), f"col {i + 1}"
    return None, "none"


def segment(blocks: list[Block], overrides: Overrides | None = None) -> list[Entry]:
    ov = overrides or Overrides()
    entries: list[Entry] = []
    for b in blocks:
        if b.pos in ov.skip:
            continue
        info: DateInfo | None
        if b.pos in ov.dates:
            forced = parse_leading_date(ov.dates[b.pos])
            info = forced and DateInfo(forced.start, forced.end, forced.precision, forced.raw, b.text)
        else:
            info = block_date(b)[0]

        if info:
            e = Entry(info.start, info.end, info.precision, info.raw, b.pos, b.kind)
            if info.rest:
                e.paragraphs.append(info.rest)
            entries.append(e)
        elif entries:
            entries[-1].paragraphs.append(b.text)
        else:  # text before the first date
            entries.append(Entry(None, None, None, None, b.pos, b.kind, [b.text]))
    return [e for e in entries if e.text.strip() or e.date_start]


@dataclass
class Report:
    blocks: int
    entries: int
    undated_entries: list[int]                 # source positions
    backwards: list[tuple[int, str, str]]      # (pos, previous date, this date)
    per_year: dict[int, int]
    precision: dict[str, int]
    table_entries: int
    empty_entries: list[int]
    future: list[tuple[int, str]] = field(default_factory=list)                 # (pos, date)
    outliers: list[tuple[int, str, str, str]] = field(default_factory=list)    # (pos, prev, this, next)

    def render(self, snippets: dict[int, str] | None = None, limit: int | None = 20) -> str:
        lines = [
            f"Blocks read:        {self.blocks}",
            f"Entries:            {self.entries}",
            f"  from table rows:  {self.table_entries}",
            f"Date precision:     " + ", ".join(f"{k}={v}" for k, v in sorted(self.precision.items())),
            f"Undated entries:    {len(self.undated_entries)}",
            f"Dates going back:   {len(self.backwards)}",
            f"Entries w/o text:   {len(self.empty_entries)}",
            f"Future dates:       {len(self.future)}",
            f"Likely typos:       {len(self.outliers)}",
            "",
            "Entries per year:",
        ]
        lines += [f"  {y}: {n}" for y, n in sorted(self.per_year.items())]

        def section(title: str, rows: list[str]) -> None:
            if not rows:
                return
            lines.extend(["", f"{title.rstrip(':')} - {len(rows)} total:", *rows[:limit]])
            if limit is not None and len(rows) > limit:
                lines.append(f"  ... and {len(rows) - limit} more (use --full-report)")

        snip = snippets or {}
        section("Undated entries (pos):",
                [f"  pos {p}" + (f"  {snip[p]!r}" if p in snip else "") for p in self.undated_entries])
        section("Dates going backwards (pos: previous -> this):",
                [f"  pos {p}: {a} -> {b}" + (f"  {snip[p]!r}" if p in snip else "")
                 for p, a, b in self.backwards])
        section("Likely date typos - far from both neighbours (pos: previous | THIS | next):",
                [f"  pos {p}: {a} | {b} | {c}" + (f"  {snip[p]!r}" if p in snip else "")
                 for p, a, b, c in self.outliers])
        section("Dates in the future (pos: date):",
                [f"  pos {p}: {d}" + (f"  {snip[p]!r}" if p in snip else "") for p, d in self.future])
        section("Dated entries without text (pos):",
                [f"  pos {p}" + (f"  {snip[p]!r}" if p in snip else "") for p in self.empty_entries])
        return "\n".join(lines)


OUTLIER_DAYS = 90


def find_outliers(dated: list[Entry], gap_days: int = OUTLIER_DAYS) -> list[tuple[int, str, str, str]]:
    """Entries far from both neighbours while the neighbours are close: likely typos (2023 among 2013)."""
    out = []
    for prev, e, nxt in zip(dated, dated[1:], dated[2:]):
        far = lambda a, b: abs((a.date_start - b.date_start).days) > gap_days
        if far(e, prev) and far(e, nxt) and not far(prev, nxt):
            out.append((e.source_pos, prev.raw_date, e.raw_date, nxt.raw_date))
    return out


def build_report(blocks: list[Block], entries: list[Entry], today: date | None = None) -> Report:
    today = today or date.today()
    backwards: list[tuple[int, str, str]] = []
    prev: Entry | None = None
    for e in entries:
        if e.date_start is None:
            continue
        # a month/year entry may legitimately sit next to day entries of the same period
        if prev and e.date_end < prev.date_start:
            backwards.append((e.source_pos, prev.raw_date, e.raw_date))
        prev = e
    return Report(
        blocks=len(blocks),
        entries=len(entries),
        undated_entries=[e.source_pos for e in entries if e.date_start is None],
        backwards=backwards,
        per_year=dict(Counter(e.date_start.year for e in entries if e.date_start)),
        precision=dict(Counter(e.precision for e in entries if e.precision)),
        table_entries=sum(e.format == "table" for e in entries),
        empty_entries=[e.source_pos for e in entries if e.date_start and not e.text.strip()],
        future=[(e.source_pos, e.raw_date) for e in entries if e.date_start and e.date_start > today],
        outliers=find_outliers([e for e in entries if e.date_start]),
    )
