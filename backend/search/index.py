"""Build / update the search index: chunks + full-text (FTS5) + vectors (sqlite-vec).

    python -m search.index            # incremental: only entries without chunks
    python -m search.index --rebuild  # everything again (e.g. after changing the embedding model)

Incremental by design: unchanged entries keep their chunks; edited entries were deleted and
re-inserted by the import, and their old chunks/vectors went with them (cascade + triggers).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path

import sqlite_vec

from ingest.store import connect

from .chunk import chunk_text

DATA = Path(__file__).resolve().parents[2] / "data"
Embedder = Callable[[list[str]], list[list[float]]]


def _meta(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT INTO index_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def has_vectors(con: sqlite3.Connection) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE name = 'chunks_vec'").fetchone() is not None


def _ensure_vec_table(con: sqlite3.Connection, dim: int) -> None:
    con.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0("
                f"embedding float[{dim}] distance_metric=cosine)")
    con.execute("CREATE TRIGGER IF NOT EXISTS chunks_vec_ad AFTER DELETE ON chunks BEGIN "
                "DELETE FROM chunks_vec WHERE rowid = old.id; END")


def reset(con: sqlite3.Connection) -> None:
    with con:
        con.execute("DROP TRIGGER IF EXISTS chunks_vec_ad")
        con.execute("DROP TABLE IF EXISTS chunks_vec")
        con.execute("DELETE FROM chunks")
        con.execute("DELETE FROM index_meta")


def embed_input(raw_date: str | None, text: str) -> str:
    """The date goes into the embedded text so questions about periods find the right chunks."""
    return f"Datum: {raw_date}\n{text}" if raw_date else text


def update_index(con: sqlite3.Connection, embed: Embedder, model: str, batch_chunks: int = 48,
                 progress: Callable[[int, int], None] | None = None) -> int:
    """Chunk and embed every entry that has no chunks yet. Returns the number of new chunks."""
    if (_meta(con, "embed_model") or model) != model:
        reset(con)  # vectors from different models can't be mixed

    todo = con.execute(
        "SELECT e.id, e.raw_date, e.text FROM entries e "
        "WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.entry_id = e.id) ORDER BY e.id").fetchall()
    total, done = len(todo), 0
    group: list[tuple[int, int, str, str]] = []  # (entry_id, seq, text, embed_text)

    def flush() -> None:
        nonlocal group
        if not group:
            return
        vectors = embed([g[3] for g in group])
        dim = len(vectors[0])
        with con:
            if not has_vectors(con):
                _ensure_vec_table(con, dim)
                _set_meta(con, "embed_model", model)
                _set_meta(con, "embed_dim", str(dim))
            elif _meta(con, "embed_dim") != str(dim):
                raise RuntimeError(f"Embedding size changed ({_meta(con, 'embed_dim')} -> {dim}); use --rebuild.")
            for (entry_id, seq, text, _), vec in zip(group, vectors):
                cur = con.execute("INSERT INTO chunks (entry_id, seq, text) VALUES (?, ?, ?)",
                                  (entry_id, seq, text))
                con.execute("INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
                            (cur.lastrowid, sqlite_vec.serialize_float32(vec)))
        group = []

    new_chunks = 0
    for entry_id, raw_date, text in todo:
        pieces = chunk_text(text)
        group += [(entry_id, i, p, embed_input(raw_date, p)) for i, p in enumerate(pieces)]
        new_chunks += len(pieces)
        done += 1
        if len(group) >= batch_chunks:  # whole entries per transaction: no half-indexed entry
            flush()
            if progress:
                progress(done, total)
    flush()
    if progress and total:
        progress(total, total)
    return new_chunks


def main(argv: list[str] | None = None) -> None:
    from .ollama_client import EMBED_MODEL, OllamaError, embed, require

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DATA / "corpus.db")
    ap.add_argument("--model", default=EMBED_MODEL)
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args(argv)
    if not args.db.exists():
        raise SystemExit(f"{args.db} not found. Open the vault and run the import first.")
    try:
        require(args.model)
    except OllamaError as e:
        raise SystemExit(str(e))

    con = connect(args.db)
    if args.rebuild:
        reset(con)
    start = time.time()

    def progress(done: int, total: int) -> None:
        print(f"\r  indexed {done}/{total} entries", end="", file=sys.stderr, flush=True)

    n = update_index(con, lambda texts: embed(texts, args.model), args.model, progress=progress)
    chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    print(f"\nIndex: {n} new chunks in {time.time() - start:.0f}s, {chunks} chunks total ({args.model})")


if __name__ == "__main__":
    main()
