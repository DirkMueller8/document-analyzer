"""Hybrid retrieval: full-text (exact names, keywords) + vectors (meaning), merged with RRF."""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

import sqlite_vec

from .index import has_vectors

RRF_K = 60
STOPWORDS = set("""
der die das den dem des ein eine einen einem einer und oder aber ich mich mir mein meine meinen wir uns
du er sie es ihr ihm ihn was wann wie wer wo warum welche welcher welches habe hat hatte hatten bin bist
ist war waren sind sein zum zur zu im in an am auf aus bei mit von vom für über unter nach vor seit bis
nicht kein keine auch noch schon sehr mal erste ersten erstes erstmals zum denn dass als wenn ob so
the a an and or of to in on at for with from by is was were be been i me my we you he she it they
when what who where why how did do does have has had first time
""".split())


@dataclass
class Hit:
    chunk_id: int
    entry_id: int
    date_start: str | None
    raw_date: str | None
    text: str
    score: float
    sources: list[str] = field(default_factory=list)  # "fts" / "vec"


def fts_query(question: str) -> str | None:
    """OR-query of the meaningful words, each quoted so FTS syntax in the question can't break it."""
    words = [w for w in re.findall(r"\w+", question) if len(w) > 1 and w.lower() not in STOPWORDS]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words)) or None


def _date_clause(date_from: date | None, date_to: date | None) -> tuple[str, list]:
    sql, params = "", []
    if date_from:
        sql += " AND e.date_end >= ?"
        params.append(date_from.isoformat())
    if date_to:
        sql += " AND e.date_start <= ?"
        params.append(date_to.isoformat())
    return sql, params


def search(con: sqlite3.Connection, question: str, embed_query, k: int = 12, pool: int = 60,
           date_from: date | None = None, date_to: date | None = None) -> list[Hit]:
    scores: dict[int, float] = defaultdict(float)
    sources: dict[int, list[str]] = defaultdict(list)
    date_sql, date_params = _date_clause(date_from, date_to)

    if q := fts_query(question):
        rows = con.execute(
            "SELECT c.id FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
            "JOIN entries e ON e.id = c.entry_id "
            f"WHERE chunks_fts MATCH ?{date_sql} ORDER BY bm25(chunks_fts) LIMIT ?",
            [q, *date_params, pool]).fetchall()
        for rank, (cid,) in enumerate(rows):
            scores[cid] += 1 / (RRF_K + rank)
            sources[cid].append("fts")

    if embed_query and has_vectors(con):
        vec = sqlite_vec.serialize_float32(embed_query(question))
        knn = pool * 5 if (date_from or date_to) else pool  # over-fetch, then filter by date
        rows = con.execute(
            "SELECT v.rowid FROM chunks_vec v JOIN chunks c ON c.id = v.rowid "
            "JOIN entries e ON e.id = c.entry_id "
            f"WHERE v.embedding MATCH ? AND v.k = ?{date_sql} ORDER BY v.distance",
            [vec, knn, *date_params]).fetchall()
        for rank, (cid,) in enumerate(rows[:pool]):
            scores[cid] += 1 / (RRF_K + rank)
            sources[cid].append("vec")

    top = sorted(scores, key=scores.get, reverse=True)[:k]
    if not top:
        return []
    details = {r[0]: r for r in con.execute(
        f"SELECT c.id, c.entry_id, e.date_start, e.raw_date, c.text FROM chunks c "
        f"JOIN entries e ON e.id = c.entry_id WHERE c.id IN ({','.join('?' * len(top))})", top)}
    return [Hit(cid, details[cid][1], details[cid][2], details[cid][3], details[cid][4], scores[cid],
                sources[cid]) for cid in top]


def first_mentions(con: sqlite3.Connection, term: str, n: int = 5) -> list[Hit]:
    """Earliest entries containing the exact term (phrase search, diacritics-insensitive)."""
    phrase = '"' + term.replace('"', "") + '"'
    rows = con.execute(
        "SELECT c.id, c.entry_id, e.date_start, e.raw_date, c.text FROM chunks_fts "
        "JOIN chunks c ON c.id = chunks_fts.rowid JOIN entries e ON e.id = c.entry_id "
        "WHERE chunks_fts MATCH ? AND e.date_start IS NOT NULL "
        "ORDER BY e.date_start, c.seq LIMIT ?", (phrase, n)).fetchall()
    return [Hit(r[0], r[1], r[2], r[3], r[4], 0.0, ["first"]) for r in rows]
