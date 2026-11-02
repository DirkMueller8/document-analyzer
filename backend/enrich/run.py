"""Extract mood, people, health events, activities and milestones per entry with the LOCAL model.

    python -m enrich.run                     # all entries not yet extracted with this version
    python -m enrich.run --limit 20          # a short test run
    python -m enrich.run --from 2008 --to 2010 --limit 20 --spread   # 20 entries spread over 2008-2010
    python -m enrich.run --mood-only         # only the mood of every dated entry: faster, for trends over the years
    python -m enrich.run --stats             # counts of what is stored, no model call
    python -m enrich.run --show 20           # results of the last 20 entries (private content!)

Resumable: every entry is written in its own transaction together with the text hash, the model
and an extraction version (schema version + fingerprint of the prompt, which includes the taxonomy).
Stop it at any time; the next run continues where it stopped; a changed prompt or taxonomy redoes all. Edited entries get new ids at sync and are extracted again.
The output contains counts only, never diary text.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from ingest.store import connect

from .extract import MAX_CHARS, MOOD_SYSTEM, ChatFn, Checks, extract_entry, extract_mood, system_prompt
from .notes import DEFAULT as NOTES, Note, context_for, context_tag, load_notes
from .schema import _Extraction, build_model
from .taxonomy import Domain, load_domains

DATA = Path(__file__).resolve().parents[2] / "data"
SCHEMA_VERSION = 6  # 2: people relation; 3: + not a person; 4: mood required; 5: mood_reason, no "other";
#                     6: milestones may have the domain "general"
CHILD_TABLES = ("mentions", "health_events", "activities", "milestones")
MAX_FAILURES_IN_A_ROW = 5


def version(domains: list[Domain]) -> str:
    """Schema version + fingerprint of the full prompt (which includes the taxonomy)."""
    prompt = hashlib.sha256(system_prompt(domains).encode("utf-8")).hexdigest()[:8]
    return f"{SCHEMA_VERSION}:{prompt}"


MOOD_VERSION = 1  # the mood-only pass has its own number


def mood_version() -> str:
    """Version of the mood-only pass: its own number + fingerprint of its prompt."""
    return f"mood{MOOD_VERSION}:{hashlib.sha256(MOOD_SYSTEM.encode('utf-8')).hexdigest()[:8]}"


def ensure_columns(con: sqlite3.Connection) -> None:
    """Databases created before phase 3 lack the bookkeeping columns of entry_meta."""
    have = {row[1] for row in con.execute("PRAGMA table_info(entry_meta)")}
    with con:
        for col in ("model", "version", "extracted_at"):
            if col not in have:
                con.execute(f"ALTER TABLE entry_meta ADD COLUMN {col} TEXT")


def pending(con: sqlite3.Connection, ver: str, date_from: date | None = None,
            date_to: date | None = None) -> list[tuple[int, str | None, str, str]]:
    """Entries not yet extracted with this version; with a date range only dated entries that start in it."""
    sql = ("SELECT e.id, e.raw_date, e.text, e.text_hash FROM entries e LEFT JOIN entry_meta m ON m.entry_id = e.id "
           "WHERE (m.entry_id IS NULL OR m.text_hash != e.text_hash OR m.version IS NOT ?)")
    params: list = [ver]
    if date_from:
        sql += " AND e.date_start >= ?"
        params.append(date_from.isoformat())
    if date_to:
        sql += " AND e.date_start <= ?"
        params.append(date_to.isoformat())
    return con.execute(sql + " ORDER BY e.date_start, e.id", params).fetchall()


def spread(items: list, n: int) -> list:
    """n items evenly spaced over the list (all of them if there are fewer)."""
    if n >= len(items):
        return items
    return [items[i * len(items) // n] for i in range(n)]


class People:
    """Name -> person id, matched case-insensitively on the canonical name and the aliases.

    Names on the ignore list (people_ignore) map to None and are never stored.
    """

    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con
        self.ids: dict[str, int] = {}
        self.ignored = {r[0] for r in con.execute("SELECT name_key FROM people_ignore")}
        for id_, name, aliases in con.execute("SELECT id, canonical_name, aliases FROM people"):
            for n in [name, *json.loads(aliases or "[]")]:
                self.ids.setdefault(n.casefold(), id_)

    def id_for(self, name: str) -> int | None:
        key = name.casefold()
        if key in self.ignored:
            return None
        if key not in self.ids:
            self.ids[key] = self.con.execute("INSERT INTO people (canonical_name) VALUES (?)", (name,)).lastrowid
        return self.ids[key]


def store(con: sqlite3.Connection, people: People, entry_id: int, text_hash: str, result: _Extraction,
          model: str, ver: str) -> None:
    with con:
        for table in CHILD_TABLES:
            con.execute(f"DELETE FROM {table} WHERE entry_id = ?", (entry_id,))
        con.execute(
            "INSERT OR REPLACE INTO entry_meta (entry_id, text_hash, mood_score, emotions, energy, sleep, themes,"
            " model, version, extracted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entry_id, text_hash, result.mood_score, json.dumps(result.emotions, ensure_ascii=False),
             result.energy, result.sleep, json.dumps(result.themes, ensure_ascii=False), model, ver,
             datetime.now(timezone.utc).isoformat(timespec="seconds")))
        for pid in dict.fromkeys(people.id_for(n) for n in result.people):
            if pid is not None:
                con.execute("INSERT OR IGNORE INTO mentions (person_id, entry_id) VALUES (?, ?)", (pid, entry_id))
        con.executemany(
            "INSERT INTO health_events (entry_id, condition, category, type, severity) VALUES (?, ?, ?, ?, ?)",
            [(entry_id, h.condition, h.category, h.type, h.severity) for h in result.health_events])
        con.executemany(
            "INSERT INTO activities (entry_id, domain, activity, duration_min, intensity, sentiment)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(entry_id, a.domain, a.activity, a.duration_min, a.intensity, a.sentiment) for a in result.activities])
        con.executemany(
            "INSERT INTO milestones (entry_id, domain, title, kind) VALUES (?, ?, ?, ?)",
            [(entry_id, m.domain, m.title, m.kind) for m in result.milestones])


@dataclass
class RunStats:
    done: int = 0
    failed: int = 0
    pending: int = 0
    eval_tokens: int = 0
    eval_seconds: float = 0.0
    checks: Checks = field(default_factory=Checks)
    failed_ids: list[int] = field(default_factory=list)

    def __str__(self) -> str:
        rate = f", {self.eval_tokens / self.eval_seconds:.1f} tokens/s" if self.eval_seconds else ""
        return (f"{self.done} extracted, {self.failed} failed, {self.pending - self.done - self.failed} left; "
                f"checks dropped {self.checks.durations_dropped} unstated durations and "
                f"{self.checks.people_dropped} names not in the text; skipped {self.checks.public_skipped} "
                f"names of public or only watched people, teams, places and the like; "
                f"{self.checks.pieces} model calls, {self.eval_tokens} tokens generated{rate}")


def enrich(con: sqlite3.Connection, chat: ChatFn, domains: list[Domain], model: str, limit: int | None = None,
           max_chars: int = MAX_CHARS, progress: Callable[[RunStats], None] | None = None,
           date_from: date | None = None, date_to: date | None = None, even: bool = False) -> RunStats:
    """Extract every pending entry (in the date range; with `even`, `limit` entries spread over it).

    Failures are counted (by id) and retried on the next run.
    """
    ensure_columns(con)
    ver = version(domains)
    schema_model = build_model([d.name for d in domains])
    system = system_prompt(domains)
    todo = pending(con, ver, date_from, date_to)
    stats = RunStats(pending=len(todo))
    if limit:
        todo = spread(todo, limit) if even else todo[:limit]
    people = People(con)
    in_a_row = 0
    for entry_id, raw_date, text, text_hash in todo:
        try:
            result, checks, calls = extract_entry(text, raw_date, chat, schema_model, system, max_chars)
        except (ValidationError, ValueError):
            stats.failed += 1
            stats.failed_ids.append(entry_id)
            in_a_row += 1
            if in_a_row >= MAX_FAILURES_IN_A_ROW:
                raise RuntimeError(f"{in_a_row} entries in a row did not fit the schema; stopping.") from None
            continue
        in_a_row = 0
        try:
            store(con, people, entry_id, text_hash, result, model, ver)
        except BaseException:
            people = People(con)  # the rollback may have removed people the cache already holds
            raise
        stats.done += 1
        stats.checks.add(checks)
        for c in calls:
            stats.eval_tokens += c.get("eval_count") or 0
            stats.eval_seconds += (c.get("eval_duration") or 0) / 1e9
        if progress:
            progress(stats)
    with con:  # people no entry mentions any more, unless aliases were added by hand
        con.execute("DELETE FROM people WHERE aliases = '[]' AND NOT EXISTS "
                    "(SELECT 1 FROM mentions m WHERE m.person_id = people.id)")
    return stats


def pending_mood(con: sqlite3.Connection, ver: str, date_from: date | None = None, date_to: date | None = None,
                 notes: Sequence[Note] = ()) -> list[tuple[int, str | None, str, str, str]]:
    """Dated entries without any result, without a mood (early test runs stored none), or with a mood-only result of
    another version. An entry in a period with a context expects the version with that context's tag, so
    adding or changing a context redoes exactly those entries. Full extractions that have a mood stay as they are.
    Rows: (id, raw date, text, text hash, ISO date)."""
    sql = ("SELECT e.id, e.raw_date, e.text, e.text_hash, e.date_start, m.entry_id, m.text_hash, m.mood_score, "
           "m.version FROM entries e LEFT JOIN entry_meta m ON m.entry_id = e.id WHERE e.date_start IS NOT NULL")
    params: list = []
    if date_from:
        sql += " AND e.date_start >= ?"
        params.append(date_from.isoformat())
    if date_to:
        sql += " AND e.date_start <= ?"
        params.append(date_to.isoformat())
    out = []
    for entry_id, raw, text, h, iso, meta_id, meta_hash, meta_mood, meta_ver in con.execute(
            sql + " ORDER BY e.date_start, e.id", params):
        want = ver + context_tag(context_for(notes, iso))
        if (meta_id is None or meta_hash != h or meta_mood is None
                or (str(meta_ver or "").startswith("mood") and meta_ver != want)):
            out.append((entry_id, raw, text, h, iso))
    return out


def store_mood(con: sqlite3.Connection, entry_id: int, text_hash: str, score: int, model: str, ver: str) -> None:
    with con:
        con.execute("INSERT OR REPLACE INTO entry_meta (entry_id, text_hash, mood_score, model, version, extracted_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (entry_id, text_hash, score, model, ver, datetime.now(timezone.utc).isoformat(timespec="seconds")))


def enrich_mood(con: sqlite3.Connection, chat: ChatFn, model: str, limit: int | None = None,
                progress: Callable[[RunStats], None] | None = None, date_from: date | None = None,
                date_to: date | None = None, even: bool = False, max_chars: int = MAX_CHARS,
                notes: Sequence[Note] = ()) -> RunStats:
    """Estimate only the mood of every pending dated entry: faster than the full extraction, resumable.

    Entries that already have a result from the full extraction are skipped. A later full run
    replaces mood-only results, because their version differs. Entries in a period with a context
    (enrich.notes) are rated with that background and stored under a version that carries its tag.
    """
    ensure_columns(con)
    base = mood_version()
    todo = pending_mood(con, base, date_from, date_to, notes)
    stats = RunStats(pending=len(todo))
    if limit:
        todo = spread(todo, limit) if even else todo[:limit]
    in_a_row = 0
    for entry_id, raw_date, text, text_hash, iso in todo:
        context = context_for(notes, iso)
        ver = base + context_tag(context)
        try:
            score, _, calls = extract_mood(text, raw_date, chat, max_chars=max_chars, context=context or None)
        except (ValidationError, ValueError):
            stats.failed += 1
            stats.failed_ids.append(entry_id)
            in_a_row += 1
            if in_a_row >= MAX_FAILURES_IN_A_ROW:
                raise RuntimeError(f"{in_a_row} entries in a row did not fit the schema; stopping.") from None
            continue
        in_a_row = 0
        store_mood(con, entry_id, text_hash, score, model, ver)
        stats.done += 1
        stats.checks.pieces += len(calls)
        for c in calls:
            stats.eval_tokens += c.get("eval_count") or 0
            stats.eval_seconds += (c.get("eval_duration") or 0) / 1e9
        if progress:
            progress(stats)
    return stats


def describe_mood(stats: RunStats) -> str:
    rate = f", {stats.eval_tokens / stats.eval_seconds:.1f} tokens/s" if stats.eval_seconds else ""
    return (f"{stats.done} rated, {stats.failed} failed, {stats.pending - stats.done - stats.failed} left; "
            f"{stats.checks.pieces} model calls, {stats.eval_tokens} tokens generated{rate}")


def summary(con: sqlite3.Connection) -> str:
    count = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    lines = [
        f"entries: {count('SELECT COUNT(*) FROM entries')}, extracted: {count('SELECT COUNT(*) FROM entry_meta')}, "
        f"with mood: {count('SELECT COUNT(*) FROM entry_meta WHERE mood_score IS NOT NULL')}",
        f"people: {count('SELECT COUNT(*) FROM people')}, mentions: {count('SELECT COUNT(*) FROM mentions')}, "
        f"health events: {count('SELECT COUNT(*) FROM health_events')}, "
        f"milestones: {count('SELECT COUNT(*) FROM milestones')}",
        "activities per domain: " + (", ".join(
            f"{d} {n}" for d, n in con.execute("SELECT domain, COUNT(*) FROM activities GROUP BY domain ORDER BY 2 DESC"))
            or "none"),
    ]
    return "\n".join(lines)


def _signed(v: int | None) -> str:
    return "-" if v is None else f"{v:+d}"


def show(con: sqlite3.Connection, n: int = 20) -> str:
    """The stored results of the last n extracted entries, oldest first. Contains private content."""
    ids = [r[0] for r in con.execute(
        "SELECT entry_id FROM entry_meta ORDER BY extracted_at DESC, entry_id DESC LIMIT ?", (n,))]
    out = []
    for entry_id in sorted(ids):
        raw_date, mood, energy, sleep, emotions, themes = con.execute(
            "SELECT e.raw_date, m.mood_score, m.energy, m.sleep, m.emotions, m.themes FROM entries e"
            " JOIN entry_meta m ON m.entry_id = e.id WHERE e.id = ?", (entry_id,)).fetchone()
        lines = [f"[{raw_date or 'ohne Datum'}] (id {entry_id})  mood {_signed(mood)} · energy {energy or '-'}"
                 f" · sleep {sleep or '-'}"]
        for label, value in (("emotions", emotions), ("themes", themes)):
            if items := json.loads(value or "[]"):
                lines.append(f"    {label}: {', '.join(items)}")
        if people := [r[0] for r in con.execute(
                "SELECT p.canonical_name FROM mentions x JOIN people p ON p.id = x.person_id"
                " WHERE x.entry_id = ? ORDER BY p.canonical_name", (entry_id,))]:
            lines.append(f"    people: {', '.join(people)}")
        for domain, activity, minutes, intensity, sentiment in con.execute(
                "SELECT domain, activity, duration_min, intensity, sentiment FROM activities"
                " WHERE entry_id = ? ORDER BY id", (entry_id,)):
            lines.append(f"    activity: {domain} / {activity}" + (f", {minutes} min" if minutes else "")
                         + f", intensity {intensity or '-'}, enjoyed {_signed(sentiment)}")
        for condition, category, kind, severity in con.execute(
                "SELECT condition, category, type, severity FROM health_events WHERE entry_id = ? ORDER BY id",
                (entry_id,)):
            lines.append(f"    health: {condition} ({category}, {kind}, severity {severity or '-'})")
        for domain, title, kind in con.execute(
                "SELECT domain, title, kind FROM milestones WHERE entry_id = ? ORDER BY id", (entry_id,)):
            lines.append(f"    milestone: {kind} · {domain} · {title}")
        out.append("\n".join(lines))
    return "\n\n".join(out) if out else "Nothing extracted yet."


def main(argv: list[str] | None = None) -> None:
    from search.ask import parse_when
    from search.ollama_client import CHAT_MODEL, OllamaError, chat_json, require

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DATA / "corpus.db")
    ap.add_argument("--taxonomy", type=Path, help="default: data/taxonomy.yaml, else the example in config/")
    ap.add_argument("--model", default=CHAT_MODEL)
    ap.add_argument("--limit", type=int, help="stop after N entries (test runs)")
    ap.add_argument("--from", dest="date_from", help="only entries from YYYY, YYYY-MM or YYYY-MM-DD on")
    ap.add_argument("--to", dest="date_to", help="only entries up to YYYY, YYYY-MM or YYYY-MM-DD")
    ap.add_argument("--spread", action="store_true", help="with --limit: spread the N entries evenly over the range")
    ap.add_argument("--think", action="store_true", help="let the model reason before answering (slower)")
    ap.add_argument("--mood-only", action="store_true",
                    help="only estimate the mood of every dated entry (faster; a later full run replaces it)")
    ap.add_argument("--notes", type=Path, default=NOTES, help="period notes with optional context (see enrich.notes)")
    ap.add_argument("--no-context", action="store_true", help="ignore the period notes (mood-only pass)")
    ap.add_argument("--stats", action="store_true", help="only print counts of stored results")
    ap.add_argument("--show", type=int, nargs="?", const=20, metavar="N",
                    help="show the results of the last N extracted entries (default 20; private content!)")
    args = ap.parse_args(argv)
    if not args.db.exists():
        raise SystemExit(f"{args.db} not found. Open the vault and run the import first.")
    con = connect(args.db)
    if args.stats or args.show:
        ensure_columns(con)
        if args.show:
            print("Private content - for this screen only, do not paste it anywhere.\n", file=sys.stderr)
            print(show(con, args.show))
        else:
            print(summary(con))
        return
    try:
        require(args.model)
    except OllamaError as e:
        raise SystemExit(str(e))

    start = time.time()
    chat = lambda messages, schema: chat_json(messages, schema, model=args.model, think=args.think)  # noqa: E731

    def progress(s: RunStats) -> None:
        print(f"\r  {s.done + s.failed}/{min(s.pending, args.limit or s.pending)} entries "
              f"({s.failed} failed, {time.time() - start:.0f} s)", end="", file=sys.stderr, flush=True)

    if args.mood_only:
        try:
            notes = [] if args.no_context else load_notes(args.notes)
        except ValueError as e:
            raise SystemExit(f"{e}\nFix the notes file or run with --no-context.")
        if any(n.context for n in notes):
            print(f"Period notes with a context: {sum(bool(n.context) for n in notes)} "
                  "(their entries are rated again with that background)", file=sys.stderr)
        try:
            stats = enrich_mood(con, chat, args.model, limit=args.limit, progress=progress,
                                date_from=parse_when(args.date_from), date_to=parse_when(args.date_to, end=True),
                                even=args.spread, notes=notes)
        except OllamaError as e:
            raise SystemExit(f"\n{e}\nAlready rated entries are kept; run again to continue.")
        except KeyboardInterrupt:
            raise SystemExit("\nStopped. Already rated entries are kept; run again to continue.")
        print(f"\n{describe_mood(stats)} ({time.time() - start:.0f} s, model {args.model}, local, {mood_version()})")
        if stats.failed_ids:
            print(f"failed entry ids (retried next run): {stats.failed_ids[:20]}")
        return
    domains = load_domains(args.taxonomy)
    try:
        stats = enrich(con, chat, domains, args.model, limit=args.limit, progress=progress,
                       date_from=parse_when(args.date_from), date_to=parse_when(args.date_to, end=True),
                       even=args.spread)
    except OllamaError as e:
        raise SystemExit(f"\n{e}\nAlready extracted entries are kept; run again to continue.")
    except KeyboardInterrupt:
        raise SystemExit("\nStopped. Already extracted entries are kept; run again to continue.")
    print(f"\n{stats} ({time.time() - start:.0f} s, model {args.model}, local)")
    if stats.failed_ids:
        print(f"failed entry ids (retried next run): {stats.failed_ids[:20]}")


if __name__ == "__main__":
    main()
