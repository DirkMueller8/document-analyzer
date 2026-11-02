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
from difflib import SequenceMatcher
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


# A mood you rate yourself: a line "Stimmung = +2" (-5..+5) on its own line in the entry. Built in, so
# it needs no configuration; define a kind named "mood" in measurements.yaml to use other wording.
# Typographic minus signs (LibreOffice autocorrect) are accepted; lines that merely resemble the format
# ("Stimmung: 2", "Stimmung = +2,5", a marker after other text) are counted as ignored variants.
MOOD_KIND = Kind(
    "mood", None, -5.0, 5.0,
    # a table row joins its cells with a tab, so the marker may follow or precede another cell on its line
    # Text may follow the value on the same line ("Stimmung = +2 Heute war ..."); what must not follow is
    # anything that makes the number mean something else: a letter or digit (3x, 20), a decimal part (2,5),
    # a range (3-4) or a fraction (3/5). Those stay near misses.
    (re.compile(r"(?i)(?:^|\t)\s*(?:stimmung|mood)\s*=\s*(?P<value>(?:[+\-−–]\s*)?[0-5])"
                r"(?![\w/]|[.,\-–]\d)"),),
    (re.compile(r"(?i)\b(?:stimmung|mood)\b[^\w\n]{0,8}\d"),))

_KEYWORD = re.compile(r"(?i)\b(?:stimmung|mood)\b")
_AFTER_KEYWORD = re.compile(r"\s*(?P<sep>[:=]?)\s*(?P<sign>[+\-−–]?)(?P<gap>\s*)(?P<num>\d+)"
                            r"(?P<dec>[.,]\d+)?(?P<tail>.*)$")


def classify_mood_variant(line: str) -> str:
    """Why a line that resembles a mood rating is not read (a shape, never the text)."""
    m = _KEYWORD.search(line)
    if m is None:
        return "other"
    if line[:m.start()].rsplit("\t", 1)[-1].strip():
        return "text before the marker"
    s = _AFTER_KEYWORD.match(line[m.end():])
    if s is None:
        return "other characters between the word and the value"
    if s["sep"] == ":":
        return "colon instead of '='"
    if not s["sep"]:
        return "no '=' sign"
    if s["dec"]:
        return "decimal value"
    if len(s["num"]) > 1 or int(s["num"]) > 5:
        return "value outside -5..+5"
    if s["tail"].strip():
        return "text after the value"
    return "other"


def _variant_shapes(text: str, kind: "Kind", classify) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in text.split("\n"):
        if any(p.search(line) for p in kind.loose) and not any(p.search(line) for p in kind.patterns):
            shape = classify(line)
            counts[shape] = counts.get(shape, 0) + 1
    return counts


def mood_variant_shapes(text: str) -> dict[str, int]:
    """Counts per shape of the lines that resemble a mood rating but are not read."""
    return _variant_shapes(text, MOOD_KIND, classify_mood_variant)


# An achievement you mark yourself, next to the sentence that states it:
#   "Achievement = 2: Ich schloss mein Studium mit einer Abschlussprüfung erfolgreich ab."
# 1 small, 2 clear, 3 major; no other value exists. The marker starts a line (or table cell) and reads
# exactly "Achievement = 1", "Achievement = 2" or "Achievement = 3". What follows is free: a colon and the
# sentence on the same line, or the sentence on the next line (Shift+Return after the marker). Anything
# else (a plus sign, other numbers, ":" instead of "=") is only counted. Several markers in one entry are
# fine (one row each). They are the truth for tuning the model's "achievement" milestones
# (enrich.evaluate achievements).
ACHIEVEMENT_KIND = Kind(
    "achievement", None, 1.0, 3.0,
    (re.compile(r"(?i)(?:^|\t)\s*achievement\s*=\s*(?P<value>[1-3])(?![\w/]|[.,\-–]\d)"),),
    (re.compile(r"(?i)\bachievement\b"),))

_ACHIEVEMENT_KEYWORD = re.compile(r"(?i)\bachievement\b")
_ACHIEVEMENT_AFTER = re.compile(r"\s*(?P<sep>[:=]?)\s*(?P<sign>[+\-−–]?)\s*(?P<num>\d+)(?P<dec>[.,]\d+)?(?P<tail>.*)$")


def classify_achievement_variant(line: str) -> str:
    """Why a line that resembles an achievement marker is not read (a shape, never the text)."""
    m = _ACHIEVEMENT_KEYWORD.search(line)
    if m is None:
        return "other"
    if line[:m.start()].rsplit("\t", 1)[-1].strip():
        return "text before the marker"
    rest = line[m.end():]
    s = _ACHIEVEMENT_AFTER.match(rest)
    if s is None:
        return ("other characters between the word and the value" if re.search(r"\d", rest)
                else "no value on the same line")
    if s["sep"] == ":":
        return "colon instead of '='"
    if not s["sep"]:
        return "no '=' sign"
    if s["sign"]:
        return "plus sign" if s["sign"] == "+" else "minus sign"
    if s["dec"]:
        return "decimal value"
    if len(s["num"]) > 1 or not 1 <= int(s["num"]) <= 3:
        return "value outside 1..3"
    return "other"


_MARKER_WORD = re.compile(r"\s*(?P<word>[^\W\d_]{6,})\s*(?:=|:|[+\-−–]?\d)")


def _misspelled_keyword(line: str) -> bool:
    """A line (or table cell) that starts like a marker, but whose keyword is a typo of 'achievement'."""
    for cell in line.split("\t"):
        m = _MARKER_WORD.match(cell)
        word = m["word"].lower() if m else ""
        if word and word != "achievement" and SequenceMatcher(None, word, "achievement").ratio() >= 0.75:
            return True
    return False


def achievement_variant_shapes(text: str) -> dict[str, int]:
    """Counts per shape of the lines that resemble an achievement marker but are not read."""
    counts = _variant_shapes(text, ACHIEVEMENT_KIND, classify_achievement_variant)
    misspelled = sum(1 for line in text.split("\n") if _misspelled_keyword(line))
    if misspelled:
        counts["keyword misspelled"] = misspelled
    return counts


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
    cfg = (yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}) if cfg_path else {}
    kinds = [
        Kind(name, spec.get("unit"), float(spec.get("min", float("-inf"))), float(spec.get("max", float("inf"))),
             tuple(re.compile(p) for p in spec.get("patterns") or []),
             tuple(re.compile(p) for p in spec.get("loose_patterns") or []),
             tuple(re.compile(p) for p in spec.get("skip_lines") or []))
        for name, spec in (cfg.get("measurements") or {}).items()
    ]
    return [*kinds, *(b for b in (MOOD_KIND, ACHIEVEMENT_KIND) if all(k.name != b.name for k in kinds))]


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
                    raw = "".join(m.group("value").split()).replace(",", ".")  # "- 3" -> "-3"
                    value = float(raw.replace("−", "-").replace("–", "-"))
                    if k.lo <= value <= k.hi:
                        out.append(Measurement(own or entry_day, k.name, value, k.unit, "ref" if own else "entry"))
                break  # first matching pattern wins, so one value is not counted twice
    return out


def count_unstorable(texts: list[str], kinds: list[Kind]) -> int:
    """Values found in entries without a day date. They cannot be stored (a value needs a day); counts only."""
    return sum(len(extract(t, date(2000, 1, 1), kinds)) for t in texts)


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
