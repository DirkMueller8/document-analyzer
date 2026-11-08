"""Milestones over the years next to the mood: how often the local model saw an achievement, a first time,
a setback or a decision, per period and per domain. Counts only, never titles or text.

    python -m analysis.milestones_report                       # by year
    python -m analysis.milestones_report --from 2008 --by half
    python -m analysis.milestones_report --min-n 30

Per period: dated entries, the mean mood (your own rating where there is one, else the model's estimate),
the entries with an achievement found by the model (and their share), the achievements you marked
yourself ("Achievement = 2"), and the entries with a first time, a setback or a decision. "Starts per
achievement" divides the entries with a first time by those with an achievement: a high value means many
new beginnings and few completions. The model finds about two thirds of the achievements and also
reports some where none was marked, so the model's figures show a trend between periods, not an exact
count; your markers are exact. Periods with fewer entries than --min-n are listed but not ranked.
"""
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ingest.store import connect

from .mood_report import PRECISIONS, load as load_mood, period_key, summarize

DATA = Path(__file__).resolve().parents[2] / "data"
KINDS = ("achievement", "first", "setback", "decision")
CAVEAT = ("Model figures are a trend, not a count: the model finds about 2 of 3 achievements and also reports "
          "some where you marked none. Your markers are exact.")


@dataclass
class Row:
    key: str
    entries: int
    mood: float | None
    by_kind: dict[str, int]     # entries with at least one milestone of that kind
    marked: int                 # entries with an "Achievement = N" marker

    def share(self, kind: str) -> float:
        return self.by_kind[kind] / self.entries if self.entries else 0.0

    def starts_per_achievement(self) -> float | None:
        return self.by_kind["first"] / self.by_kind["achievement"] if self.by_kind["achievement"] else None


def _where(by: str, date_from: date | None, date_to: date | None) -> tuple[str, list]:
    precisions = sorted(PRECISIONS[by])
    where = f"e.date_start IS NOT NULL AND e.date_precision IN ({','.join('?' * len(precisions))})"
    params: list = list(precisions)
    if date_from:
        where += " AND e.date_start >= ?"
        params.append(date_from.isoformat())
    if date_to:
        where += " AND e.date_start <= ?"
        params.append(date_to.isoformat())
    return where, params


def period_rows(con: sqlite3.Connection, by: str = "year", date_from: date | None = None,
                date_to: date | None = None) -> list[Row]:
    where, params = _where(by, date_from, date_to)
    entries: dict[str, int] = {}
    for (iso,) in con.execute(f"SELECT e.date_start FROM entries e WHERE {where}", params):
        k = period_key(iso, by)
        entries[k] = entries.get(k, 0) + 1
    kinds: dict[str, dict[str, int]] = {k: dict.fromkeys(KINDS, 0) for k in entries}
    for _, iso, kind in con.execute(
            f"SELECT DISTINCT e.id, e.date_start, m.kind FROM milestones m JOIN entries e ON e.id = m.entry_id "
            f"WHERE {where} AND m.kind IS NOT NULL", params):
        kinds[period_key(iso, by)][kind] += 1
    marked: dict[str, int] = {}
    for _, iso in con.execute(
            f"SELECT DISTINCT e.id, e.date_start FROM measurements x JOIN entries e ON e.id = x.entry_id "
            f"WHERE {where} AND x.kind = 'achievement'", params):
        k = period_key(iso, by)
        marked[k] = marked.get(k, 0) + 1
    moods = {p.key: p.mean for p in summarize(load_mood(con, by, date_from, date_to)[0])}
    return [Row(k, entries[k], moods.get(k), kinds[k], marked.get(k, 0)) for k in sorted(entries)]


def domain_rows(con: sqlite3.Connection, date_from: date | None = None,
                date_to: date | None = None) -> list[tuple[str, dict[str, int]]]:
    """Entries per domain and kind, the domains with the most achievements first."""
    where, params = _where("year", date_from, date_to)
    counts: dict[str, dict[str, int]] = {}
    for domain, kind, n in con.execute(
            f"SELECT m.domain, m.kind, COUNT(DISTINCT m.entry_id) FROM milestones m JOIN entries e ON e.id = m.entry_id "
            f"WHERE {where} AND m.kind IS NOT NULL GROUP BY m.domain, m.kind", params):
        counts.setdefault(domain, dict.fromkeys(KINDS, 0))[kind] = n
    return sorted(counts.items(), key=lambda kv: (-kv[1]["achievement"], kv[0]))


def _ratio(v: float | None) -> str:
    return "-" if v is None else f"{v:.1f}"


def render(rows: list[Row], domains: list[tuple[str, dict[str, int]]], min_n: int) -> str:
    head = (f"{'period':<9}{'entries':>8}{'mood':>7}{'achv':>6}{'%':>6}{'marked':>8}"
            f"{'first':>7}{'setback':>9}{'decision':>10}{'starts/achv':>13}")
    out = ["Entries with a milestone found by the local model (per entry, counted once per kind)", "", head]
    for r in rows:
        thin = "  (few entries)" if r.entries < min_n else ""
        out.append(f"{r.key:<9}{r.entries:>8}{'-' if r.mood is None else f'{r.mood:+.1f}':>7}"
                   f"{r.by_kind['achievement']:>6}{100 * r.share('achievement'):>5.0f}%{r.marked:>8}"
                   f"{r.by_kind['first']:>7}{r.by_kind['setback']:>9}{r.by_kind['decision']:>10}"
                   f"{_ratio(r.starts_per_achievement()):>13}{thin}")
    total = Row("all", sum(r.entries for r in rows), None,
                {k: sum(r.by_kind[k] for r in rows) for k in KINDS}, sum(r.marked for r in rows))
    out.append(f"{'all':<9}{total.entries:>8}{'':>7}{total.by_kind['achievement']:>6}"
               f"{100 * total.share('achievement'):>5.0f}%{total.marked:>8}{total.by_kind['first']:>7}"
               f"{total.by_kind['setback']:>9}{total.by_kind['decision']:>10}{_ratio(total.starts_per_achievement()):>13}")

    big = [r for r in rows if r.entries >= min_n]
    if big:
        out += ["", f"Periods with at least {min_n} entries:"]
        best = sorted(big, key=lambda r: -r.share("achievement"))[:3]
        out.append("  most achievements per entry: " + ", ".join(
            f"{r.key} ({100 * r.share('achievement'):.0f} %)" for r in best))
        starts = sorted((r for r in big if r.starts_per_achievement() is not None),
                        key=lambda r: -r.starts_per_achievement())[:3]
        if starts:
            out.append("  most starts per achievement: " + ", ".join(
                f"{r.key} ({r.starts_per_achievement():.1f})" for r in starts))
        setbacks = sorted(big, key=lambda r: -r.share("setback"))[:3]
        out.append("  most setbacks per entry: " + ", ".join(
            f"{r.key} ({100 * r.share('setback'):.0f} %)" for r in setbacks))

    if domains:
        out += ["", "By domain (entries):", f"  {'domain':<22}{'achv':>6}{'first':>7}{'setback':>9}{'decision':>10}"
                f"{'starts/achv':>13}"]
        for name, c in domains:
            ratio = c["first"] / c["achievement"] if c["achievement"] else None
            out.append(f"  {name:<22}{c['achievement']:>6}{c['first']:>7}{c['setback']:>9}{c['decision']:>10}"
                       f"{_ratio(ratio):>13}")
    out += ["", CAVEAT]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DATA / "corpus.db")
    ap.add_argument("--by", choices=("year", "half", "quarter"), default="year")
    ap.add_argument("--from", dest="date_from", type=int, help="first year")
    ap.add_argument("--to", dest="date_to", type=int, help="last year")
    ap.add_argument("--min-n", type=int, default=20, help="periods with fewer entries are not ranked")
    args = ap.parse_args(argv)
    if not args.db.exists():
        raise SystemExit(f"{args.db} not found. Open the vault and run the import first.")
    con = connect(args.db)
    date_from = date(args.date_from, 1, 1) if args.date_from else None
    date_to = date(args.date_to, 12, 31) if args.date_to else None
    if not con.execute("SELECT 1 FROM milestones LIMIT 1").fetchone():
        raise SystemExit("No milestones stored yet. Run the extraction first: .\\enrich.ps1")
    print(render(period_rows(con, args.by, date_from, date_to), domain_rows(con, date_from, date_to), args.min_n))


if __name__ == "__main__":
    main()
