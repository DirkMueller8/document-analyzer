"""Period notes for the mood analysis: background that the entries alone cannot give.

The file data/mood_notes.yaml (inside the vault, never in git; example: config/mood_notes.example.yaml):

    notes:
      - from: 2020-03            # YYYY, YYYY-MM or YYYY-MM-DD
        to: 2020-09
        label: rehab             # optional short word, drawn into the chart
        note: ...                # shown under the report table, on your screen; changes no number
        context: ...             # optional: told to the local model as background for the entries of this period

A period with a context is estimated again with it: the mood-only pass sees that the version of the stored
estimate no longer matches and redoes just those entries. Without the file nothing changes.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

DATA = Path(__file__).resolve().parents[2] / "data"
DEFAULT = DATA / "mood_notes.yaml"


@dataclass(frozen=True)
class Note:
    start: date
    end: date
    note: str = ""
    context: str = ""
    label: str = ""

    def covers(self, d: date) -> bool:
        return self.start <= d <= self.end

    def overlaps(self, first: date, last: date) -> bool:
        return self.start <= last and self.end >= first


def _when(value, end: bool) -> date:
    from search.ask import parse_when

    if isinstance(value, date):          # YAML turns 2015-03-02 into a date
        return value
    result = parse_when(str(value), end=end)
    if result is None:
        raise ValueError(f"not a date: {value!r}")
    return result


def load_notes(path: str | Path | None = None) -> list[Note]:
    """The notes of the file, or none if there is no file. A broken entry raises ValueError with its number."""
    p = Path(path) if path else DEFAULT
    if not p.exists():
        return []
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    notes = []
    for i, item in enumerate(cfg.get("notes") or [], 1):
        try:
            start, end = _when(item["from"], end=False), _when(item["to"], end=True)
            if start > end:
                raise ValueError("'from' is after 'to'")
            notes.append(Note(start, end, str(item.get("note") or "").strip(), str(item.get("context") or "").strip(),
                              str(item.get("label") or "").strip()))
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"{p.name}, note {i}: {e if not isinstance(e, KeyError) else f'missing {e}'}") from None
    return notes


def load_chart_from(path: str | Path | None = None) -> int | None:
    """The optional top-level setting `chart_from: 1994` of the notes file: the first year of the chart."""
    p = Path(path) if path else DEFAULT
    if not p.exists():
        return None
    value = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("chart_from")
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{p.name}: chart_from must be a year, not {value!r}") from None


def context_for(notes, iso: str | None) -> str:
    """The contexts of all notes that cover this date, joined; empty if there is none."""
    if not iso or not notes:
        return ""
    d = date.fromisoformat(iso[:10])
    return " ".join(n.context for n in notes if n.context and n.covers(d))


def context_tag(context: str) -> str:
    """Appended to the version of an estimate made with a context, so a changed context redoes it."""
    return f"+c{hashlib.sha256(context.encode('utf-8')).hexdigest()[:8]}" if context else ""
