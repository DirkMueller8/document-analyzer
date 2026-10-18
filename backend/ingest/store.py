"""Write entries to the local SQLite database."""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .segment import Entry

SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open the database with the sqlite-vec extension loaded (its triggers need it on every connection)."""
    import sqlite_vec

    con = sqlite3.connect(str(db_path))
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute("PRAGMA foreign_keys = ON")
    return con


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SyncStats:
    kept: int
    added: int
    removed: int

    def __str__(self) -> str:
        return f"{self.kept} unchanged, {self.added} new/changed, {self.removed} removed"


def _row(e: Entry) -> tuple:
    return (
        e.date_start and e.date_start.isoformat(),
        e.date_end and e.date_end.isoformat(),
        e.precision, e.raw_date, e.source_pos, e.format,
    )


def sync_entries(con: sqlite3.Connection, entries: list[Entry]) -> SyncStats:
    """Bring the entries table in line with the source document, touching only what changed.

    Entries are matched by text hash, so unchanged entries keep their id and with it
    their chunks, embeddings and enrichment. Edited entries count as removed + added
    (only those get re-processed). Date fields of kept entries are refreshed, so a
    date fixed in overrides.yaml does not trigger re-processing.
    """
    existing: dict[str, list[int]] = {}
    for id_, h in con.execute("SELECT id, text_hash FROM entries ORDER BY id"):
        existing.setdefault(h, []).append(id_)

    kept: list[tuple] = []
    added: list[tuple] = []
    for e in entries:
        h = text_hash(e.text)
        if existing.get(h):
            kept.append((*_row(e), existing[h].pop(0)))
        else:
            d0, d1, prec, raw, pos, fmt = _row(e)
            added.append((d0, d1, prec, raw, e.text, h, pos, fmt))
    removed = [(i,) for ids in existing.values() for i in ids]

    with con:
        con.execute("PRAGMA foreign_keys = ON")
        con.executemany("DELETE FROM entries WHERE id = ?", removed)
        con.executemany(
            "UPDATE entries SET date_start=?, date_end=?, date_precision=?, raw_date=?,"
            " source_pos=?, format=? WHERE id=?", kept)
        con.executemany(
            "INSERT INTO entries (date_start, date_end, date_precision, raw_date, text, text_hash,"
            " source_pos, format) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", added)
    return SyncStats(len(kept), len(added), len(removed))
