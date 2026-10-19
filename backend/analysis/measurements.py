"""Extract numeric measurements (e.g. body weight) from entries with configurable patterns.

The value belongs to the entry's date, unless the same line starts with a date of its own:
"12.03. G = 80,5", "12.03 ...", "12.03.2024 ..." or "Ref_12.03.2024 ...". A day.month
without year takes the entry's year (or the previous year if that would lie after the entry).
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIGS = [ROOT / "data" / "measurements.yaml", ROOT / "config" / "measurements.example.yaml"]

LINE_DATE = re.compile(r"^\s*(?:Ref_)?(\d{1,2})\.(\d{1,2})\.?(\d{4}|\d{2})?(?!\d)")


@dataclass(frozen=True)
class Kind:
    name: str
    unit: str | None
    lo: float
    hi: float
    patterns: tuple[re.Pattern, ...]
    # Looser patterns that are only COUNTED when the strict ones don't match: lines that look
    # like a measurement but have a different format are reported (counts only), never stored.
    loose: tuple[re.Pattern, ...] = ()
    # Lines matching one of these are excluded entirely (e.g. lines that only refer to another day).
    skip: tuple[re.Pattern, ...] = ()


@dataclass(frozen=True)
class Measurement:
    date: date
    kind: str
    value: float
    unit: str | None
    source: str  # "entry" | "ref"


def load_kinds(path: str | Path | None = None) -> list[Kind]:
    candidates = [Path(path)] if path else DEFAULT_CONFIGS
    cfg_path = next((p for p in candidates if p.exists()), None)
    if cfg_path is None:
        return []
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return [
        Kind(name, spec.get("unit"), float(spec.get("min", float("-inf"))), float(spec.get("max", float("inf"))),
             tuple(re.compile(p) for p in spec.get("patterns") or []),
             tuple(re.compile(p) for p in spec.get("loose_patterns") or []),
             tuple(re.compile(p) for p in spec.get("skip_lines") or []))
        for name, spec in (cfg.get("measurements") or {}).items()
    ]


def count_ignored_variants(text: str, kinds: list[Kind]) -> dict[str, int]:
    """Lines that resemble a measurement (loose pattern) but don't match the strict format.

    Returns counts per kind, never the text, so the number is safe to report.
    """
    counts: dict[str, int] = {}
    for line in text.split("\n"):
        for k in kinds:
            if k.loose and not any(p.search(line) for p in k.patterns) and any(p.search(line) for p in k.loose):
                counts[k.name] = counts.get(k.name, 0) + 1
    return counts


def count_skipped_lines(text: str, kinds: list[Kind]) -> dict[str, int]:
    """Lines that would be a measurement but are excluded by skip_lines (counts only)."""
    counts: dict[str, int] = {}
    for line in text.split("\n"):
        for k in kinds:
            if k.skip and any(s.search(line) for s in k.skip) and any(p.search(line) for p in k.patterns):
                counts[k.name] = counts.get(k.name, 0) + 1
    return counts


def _line_date(line: str, entry_day: date) -> date | None:
    m = LINE_DATE.match(line)
    if not m:
        return None
    d, mo, y = int(m[1]), int(m[2]), m[3]
    year = (1900 + int(y) if int(y) >= 30 else 2000 + int(y)) if y and len(y) == 2 else int(y) if y else entry_day.year
    try:
        result = date(year, mo, d)
    except ValueError:
        return None
    if not y and result > entry_day + timedelta(days=1):
        try:
            result = date(year - 1, mo, d)
        except ValueError:
            return None
    return result


def extract(text: str, entry_day: date, kinds: list[Kind]) -> list[Measurement]:
    out: list[Measurement] = []
    for line in text.split("\n"):
        own = _line_date(line, entry_day)
        for k in kinds:
            if any(s.search(line) for s in k.skip):
                continue
            for pattern in k.patterns:
                matches = list(pattern.finditer(line))
                if not matches:
                    continue
                for m in matches:
                    value = float(m.group("value").replace(",", "."))
                    if k.lo <= value <= k.hi:
                        out.append(Measurement(own or entry_day, k.name, value, k.unit, "ref" if own else "entry"))
                break  # first matching pattern wins, so one value is not counted twice
    return out


def store_all(con: sqlite3.Connection, kinds: list[Kind]) -> int:
    """Re-extract all measurements (cheap: regex over the text) and replace the table."""
    rows = []
    for entry_id, d, precision, text in con.execute(
            "SELECT id, date_start, date_precision, text FROM entries WHERE date_start IS NOT NULL"):
        if precision != "day":
            continue  # a value needs a day; month/year entries are too vague
        for m in extract(text, date.fromisoformat(d), kinds):
            rows.append((entry_id, m.date.isoformat(), m.kind, m.value, m.unit, m.source))
    with con:
        con.execute("DELETE FROM measurements")
        con.executemany("INSERT INTO measurements (entry_id, date, kind, value, unit, source)"
                        " VALUES (?, ?, ?, ?, ?, ?)", rows)
    return len(rows)
