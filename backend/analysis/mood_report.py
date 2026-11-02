"""Mood over the years: your own ratings where you have them, else the local model's estimates.

    python -m analysis.mood_report                              # by year, with a chart
    python -m analysis.mood_report --by quarter --from 2008 --to 2012
    python -m analysis.mood_report --min-n 15 --chart data/mood.png

Per period: number of entries, the mean mood (-5..+5) with a 95 % interval, and the shares of
negative, zero and positive entries. A change from the previous period is only called "higher" or
"lower" when the two intervals do not overlap. Periods with fewer entries than --min-n are listed
but never interpreted. The intervals cover the sampling of entries only, not a systematic error of
the model. Plain Python for the numbers; matplotlib only for the chart.
"""
from __future__ import annotations

import argparse
import calendar
import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from enrich.notes import DEFAULT as NOTES, load_chart_from, load_notes
from ingest.store import connect

DATA = Path(__file__).resolve().parents[2] / "data"
# two-sided 95 % t values for 1..30 degrees of freedom
T95 = [12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131,
       2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042]
BANDS = ("year", "half", "quarter", "month")
# an entry known only by its year cannot be put into a half year or a month
PRECISIONS = {"year": {"day", "month", "year"}, "half": {"day", "month"}, "quarter": {"day", "month"},
              "month": {"day", "month"}}
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"   # as in the weight chart
BLUE, BLUE_BAND = "#2a78d6", "#cde2fb"


@dataclass(frozen=True)
class Period:
    key: str
    n: int
    rated: int
    mean: float
    low: float | None
    high: float | None
    negative: float
    zero: float
    positive: float


def period_key(iso: str, by: str) -> str:
    year, month = iso[:4], int(iso[5:7])
    return {"year": year, "half": f"{year} H{1 + (month > 6)}", "quarter": f"{year} Q{(month - 1) // 3 + 1}",
            "month": f"{year}-{month:02d}"}[by]


def load(con: sqlite3.Connection, by: str, date_from: date | None = None,
         date_to: date | None = None) -> tuple[list[tuple[str, float, str]], int]:
    """(period, mood, source) per entry with a mood, and the number of dated entries in the range."""
    where, params = "date_start IS NOT NULL", []
    if date_from:
        where += " AND date_start >= ?"
        params.append(date_from.isoformat())
    if date_to:
        where += " AND date_start <= ?"
        params.append(date_to.isoformat())
    rows = con.execute(f"SELECT date_start, date_precision, mood, source FROM entry_mood WHERE {where}", params)
    out, dated = [], 0
    for iso, precision, mood, source in rows:
        if precision in PRECISIONS[by]:
            dated += 1
            if mood is not None:
                out.append((period_key(iso, by), float(mood), source))
    return out, dated


def summarize(rows: list[tuple[str, float, str]]) -> list[Period]:
    groups: dict[str, list[tuple[float, str]]] = {}
    for key, mood, source in rows:
        groups.setdefault(key, []).append((mood, source))
    periods = []
    for key in sorted(groups):
        values = [m for m, _ in groups[key]]
        n = len(values)
        mean = sum(values) / n
        low = high = None
        if n >= 2:
            sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
            half = (T95[n - 2] if n - 1 <= 30 else 1.96) * sd / math.sqrt(n)
            low, high = mean - half, mean + half
        periods.append(Period(key, n, sum(s == "rated" for _, s in groups[key]), mean, low, high,
                              sum(v <= -0.5 for v in values) / n, sum(-0.5 < v < 0.5 for v in values) / n,
                              sum(v >= 0.5 for v in values) / n))
    return periods


def change(prev: Period | None, cur: Period, min_n: int) -> str:
    """'higher' / 'lower' only when both periods are large enough and their intervals do not overlap."""
    if prev is None or min(prev.n, cur.n) < min_n or None in (prev.low, cur.low):
        return ""
    if cur.low > prev.high:
        return "higher"
    if cur.high < prev.low:
        return "lower"
    return "same"


def usable(p: Period, min_n: int) -> bool:
    return p.n >= min_n and p.low is not None


def comparisons(periods: list[Period], min_n: int) -> list[tuple[Period, Period | None, str]]:
    """(period, the earlier period it is compared with, verdict). The reference is the last earlier period
    with enough entries, so thin years in between do not hide a change."""
    out, prev = [], None
    for p in periods:
        out.append((p, prev, change(prev, p, min_n)))
        if usable(p, min_n):
            prev = p
    return out


def period_bounds(key: str, by: str) -> tuple[date, date]:
    """First and last day of a period key as produced by period_key."""
    year = int(key[:4])
    if by == "year":
        return date(year, 1, 1), date(year, 12, 31)
    if by == "half":
        return (date(year, 1, 1), date(year, 6, 30)) if key.endswith("H1") else (date(year, 7, 1), date(year, 12, 31))
    if by == "quarter":
        q = int(key[-1])
        return date(year, 3 * q - 2, 1), date(year, 3 * q, calendar.monthrange(year, 3 * q)[1])
    month = int(key[5:7])
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def note_marks(periods: list[Period], by: str, notes) -> tuple[dict[str, str], list[str]]:
    """Periods that a note covers (marked '#' in the table) and the note lines to print below it."""
    marks, lines = {}, []
    for n in notes:
        hit = [p for p in periods if n.overlaps(*period_bounds(p.key, by))]
        if not hit:
            continue
        marks.update({p.key: "#" for p in hit})
        lines.append(f"# {n.start:%Y-%m} to {n.end:%Y-%m}: {n.note or '(no text)'}"
                     + (" The model was told this background." if n.context else ""))
    return marks, lines


def render_table(periods: list[Period], min_n: int, marks: dict[str, str] | None = None) -> str:
    marks = marks or {}
    lines = [f"{'period':<9}{'entries':>8}{'rated':>7}{'mean':>7}  {'95 % interval':<16}"
             f"{'negative':>9}{'zero':>6}{'positive':>9}  compared with the last period that has enough entries"]
    for p, prev, verdict in comparisons(periods, min_n):
        few = p.n < min_n
        interval = "-" if few or p.low is None else f"[{p.low:+.1f}, {p.high:+.1f}]"
        note = f"{ {'higher': 'higher than', 'lower': 'lower than', 'same': 'same as'}[verdict] } {prev.key}" if verdict else ""
        lines.append(f"{p.key + marks.get(p.key, ''):<9}{p.n:>8}{p.rated:>7}{p.mean:>+6.1f}{'*' if few else ' '} {interval:<16}"
                     f"{p.negative:>9.0%}{p.zero:>6.0%}{p.positive:>9.0%}  {note}")
    if any(p.n < min_n for p in periods):
        lines.append(f"* fewer than {min_n} entries: shown, not interpreted")
    return "\n".join(lines)


def conclusions(periods: list[Period], min_n: int) -> list[str]:
    enough = [p for p in periods if usable(p, min_n)]
    if len(enough) < 2:
        return [f"Fewer than two periods have at least {min_n} entries: no comparison."]
    best, worst = max(enough, key=lambda p: p.mean), min(enough, key=lambda p: p.mean)
    out = [f"highest period {best.key} ({best.mean:+.1f}), lowest {worst.key} ({worst.mean:+.1f}): "
           + ("the intervals do not overlap, so the difference is clear." if best.low > worst.high else
              "the intervals overlap, so this much data cannot tell them apart.")]
    verdicts = [v for _, _, v in comparisons(periods, min_n) if v]
    out.append(f"clear changes: {sum(v in ('higher', 'lower') for v in verdicts)} of {len(verdicts)} comparisons "
               f"between periods with at least {min_n} entries")
    return out


CAVEATS = [
    "Entries are the days you wrote about, not all days; a period with few or unusual entries says little.",
    "Your own ratings replace the model's estimate for those entries; the rest are estimates (direction right "
    "in about 84 % of the cases where the model commits, but mild days often come out as 0).",
    "The intervals cover which entries happen to exist, not a systematic error of the model.",
]


def from_year(periods: list[Period], year: int | None) -> list[Period]:
    """The periods from this year on (all if no year is given); the table keeps showing everything."""
    return periods if year is None else [p for p in periods if int(p.key[:4]) >= year]


def save_png(fig, path: Path) -> Path:
    """Write the figure via a temporary file. If `path` cannot be replaced (typically: the old chart is open in a
    viewer), write next to it under a timestamped name. Returns the path that was written."""
    last: OSError | None = None
    for target in (path, path.with_name(f"{path.stem}_{datetime.now():%Y%m%d-%H%M%S}{path.suffix}")):
        tmp = target.with_name(target.name + ".tmp")
        try:
            with open(tmp, "wb") as f:
                fig.savefig(f, facecolor=SURFACE, dpi=150, format="png")
            os.replace(tmp, target)
            return target
        except OSError as e:
            last = e
            tmp.unlink(missing_ok=True)
    raise last


def render_chart(periods: list[Period], by: str, min_n: int, path: Path, notes=()) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    nan = float("nan")
    x = list(range(len(periods)))
    solid = [usable(p, min_n) for p in periods]
    means = [p.mean if s else nan for p, s in zip(periods, solid)]
    lows = [p.low if s else nan for p, s in zip(periods, solid)]
    highs = [p.high if s else nan for p, s in zip(periods, solid)]
    fig, (ax, axn) = plt.subplots(2, 1, figsize=(10, 6), sharex=True,
                                  gridspec_kw={"height_ratios": [3, 1], "hspace": 0.1})
    fig.patch.set_facecolor(SURFACE)
    for a in (ax, axn):
        a.set_facecolor(SURFACE)
        for side in ("top", "right", "left"):
            a.spines[side].set_visible(False)
        a.spines["bottom"].set_color(GRID)
        a.tick_params(colors=INK_2, length=0, labelsize=9)
        a.grid(axis="y", color=GRID, linewidth=0.8)
        a.set_axisbelow(True)
    ends = [v for v in (*lows, *highs) if v == v] or [abs(p.mean) for p in periods] or [2.0]
    lim = max(2.0, math.ceil(max(abs(v) for v in ends)) + 0.5)
    # one interval bar per period, so a period without neighbours shows its interval too
    ax.vlines([i for i, s in zip(x, solid) if s], [v for v in lows if v == v], [v for v in highs if v == v],
              color=BLUE_BAND, linewidth=6, capstyle="round", zorder=1)
    ax.plot(x, means, color=BLUE, linewidth=2, marker="o", markersize=6, markeredgecolor=SURFACE,
            markeredgewidth=1.5, zorder=2)
    sparse = [(i, p.mean) for i, (p, s) in enumerate(zip(periods, solid)) if not s]
    if sparse:
        ax.plot([i for i, _ in sparse], [m for _, m in sparse], linestyle="none", marker="o", markersize=6,
                markerfacecolor=SURFACE, markeredgecolor=INK_2, markeredgewidth=1.2)
    ax.axhline(0, color=INK_2, linewidth=0.8)
    ax.set_ylim(-lim, lim)
    banded = False
    for n in notes:                                      # grey band behind the periods a note covers
        hit = [i for i, p in enumerate(periods) if n.overlaps(*period_bounds(p.key, by))]
        if hit:
            banded = True
            for a in (ax, axn):
                a.axvspan(min(hit) - 0.5, max(hit) + 0.5, color=GRID, alpha=0.6, linewidth=0, zorder=0)
            if n.label:
                ax.text((min(hit) + max(hit)) / 2, lim * 0.94, n.label, ha="center", va="top", color=INK_2, fontsize=9)
    enough = [(i, p) for i, (p, s) in enumerate(zip(periods, solid)) if s]
    if len(enough) >= 2:
        for (i, p), offset in ((max(enough, key=lambda t: t[1].mean), 10), (min(enough, key=lambda t: t[1].mean), -16)):
            ax.annotate(f"{p.key}  {p.mean:+.1f}", (i, p.mean), textcoords="offset points", xytext=(0, offset),
                        ha="center", color=INK, fontsize=9)
    axn.bar(x, [p.n for p in periods], color=BLUE, width=0.7)
    axn.set_ylabel("entries", color=INK_2, fontsize=9)
    step = max(1, math.ceil(len(periods) / 14))
    axn.set_xticks(x[::step])
    axn.set_xticklabels([p.key for p in periods][::step], rotation=0 if by == "year" else 45,
                        ha="center" if by == "year" else "right")
    fig.text(0.01, 0.975, f"Mood by {by} (-5 ... +5): mean with 95 % interval", ha="left", va="top", color=INK,
             fontsize=12, weight="bold")
    fig.text(0.01, 0.935, f"Hollow markers: fewer than {min_n} entries. Estimates by the local model; "
             "your own ratings replace them where you have one." + (" Grey: periods with a note." if banded else ""),
             ha="left", va="top", color=INK_2, fontsize=9)
    fig.subplots_adjust(top=0.88, left=0.07, right=0.98, bottom=0.12)
    try:
        return save_png(fig, path)
    finally:
        plt.close(fig)


def _date_filter(date_from: date | None, date_to: date | None) -> tuple[str, list]:
    where, params = "e.date_start IS NOT NULL AND m.mood_score IS NOT NULL", []
    if date_from:
        where += " AND e.date_start >= ?"
        params.append(date_from.isoformat())
    if date_to:
        where += " AND e.date_start <= ?"
        params.append(date_to.isoformat())
    return where, params


def _rated_pairs(con: sqlite3.Connection, by: str, date_from: date | None = None,
                 date_to: date | None = None) -> dict[str, list[tuple[float, float]]]:
    """Per period: (your rating, the model's estimate) for every entry you rated."""
    where, params = _date_filter(date_from, date_to)
    rows = con.execute(
        "SELECT e.date_start, e.date_precision, r.value, m.mood_score FROM entries e "
        "JOIN (SELECT entry_id, AVG(value) AS value FROM measurements WHERE kind = 'mood' GROUP BY entry_id) r "
        f"ON r.entry_id = e.id JOIN entry_meta m ON m.entry_id = e.id WHERE {where}", params)
    groups: dict[str, list[tuple[float, float]]] = {}
    for iso, precision, rated, model in rows:
        if precision in PRECISIONS[by]:
            groups.setdefault(period_key(iso, by), []).append((float(rated), float(model)))
    return groups


def rated_check(con: sqlite3.Connection, by: str, date_from: date | None = None,
                date_to: date | None = None) -> list[tuple[str, int, float, float]]:
    """Per period: (period, entries you rated, your mean, the model's mean estimate for the same entries)."""
    groups = _rated_pairs(con, by, date_from, date_to)
    return [(key, len(v), sum(r for r, _ in v) / len(v), sum(m for _, m in v) / len(v))
            for key, v in sorted(groups.items())]


MIN_RATED = 5   # rated days a period needs before its mean is corrected by them


@dataclass(frozen=True)
class Adjusted:
    key: str
    rated: int
    estimates: float      # mean of the model's estimates over ALL entries of the period
    correction: float     # average of (your rating - the model's estimate) on the days you rated
    value: float
    low: float
    high: float


def _variance(values: list[float]) -> float:
    mean = sum(values) / len(values)
    return sum((v - mean) ** 2 for v in values) / (len(values) - 1) if len(values) > 1 else 0.0


def adjusted_estimates(con: sqlite3.Connection, by: str, date_from: date | None = None, date_to: date | None = None,
                       min_rated: int = MIN_RATED) -> list[Adjusted]:
    """Mean of all the model's estimates in a period, plus the average amount by which the model was off on the
    days you rated there (a difference estimator). The interval adds the uncertainty of that correction. It is
    only as good as the rated days are typical for the period."""
    pairs = _rated_pairs(con, by, date_from, date_to)
    where, params = _date_filter(date_from, date_to)
    estimates: dict[str, list[float]] = {}
    for iso, precision, model in con.execute(
            "SELECT e.date_start, e.date_precision, m.mood_score FROM entries e "
            f"JOIN entry_meta m ON m.entry_id = e.id WHERE {where}", params):
        if precision in PRECISIONS[by]:
            estimates.setdefault(period_key(iso, by), []).append(float(model))
    out = []
    for key, rated in sorted(pairs.items()):
        everything = estimates.get(key, [])
        if len(rated) < min_rated or len(everything) < len(rated):
            continue
        diffs = [r - m for r, m in rated]
        correction, base = sum(diffs) / len(diffs), sum(everything) / len(everything)
        se = math.sqrt(_variance(everything) / len(everything) + _variance(diffs) / len(diffs))
        half = (T95[len(diffs) - 2] if len(diffs) - 1 <= 30 else 1.96) * se
        out.append(Adjusted(key, len(diffs), base, correction, base + correction, base + correction - half,
                            base + correction + half))
    return out


def render_adjusted(rows: list[Adjusted]) -> str:
    if not rows:
        return ""
    lines = ["Corrected by your rated days (mean of ALL the model's estimates in the period plus the average amount by "
             "which the model was off on the days you rated; the interval includes the uncertainty of that correction):",
             f"{'period':<9}{'rated':>7}{'estimates':>11}{'correction':>12}{'corrected':>11}  95 % interval"]
    lines += [f"{a.key:<9}{a.rated:>7}{a.estimates:>+11.1f}{a.correction:>+12.1f}{a.value:>+11.1f}  "
              f"[{a.low:+.1f}, {a.high:+.1f}]" for a in rows]
    lines.append("Only trustworthy if the days you rated are typical for the period; the interval cannot show that.")
    return "\n".join(lines)


def render_check(rows: list[tuple[str, int, float, float]], min_rows: int = 3) -> str:
    total = sum(n for _, n, _, _ in rows)
    if not total:
        return ""
    bias = sum(n * (m - r) for _, n, r, m in rows) / total
    lines = ["Check against your own ratings (the model's estimate for the same entries you rated):",
             f"{'period':<9}{'rated':>7}{'your mean':>11}{'model mean':>12}{'model - you':>13}"]
    lines += [f"{key:<9}{n:>7}{r:>+11.1f}{m:>+12.1f}{m - r:>+13.1f}" for key, n, r, m in rows if n >= min_rows]
    lines.append(f"all periods: {total} entries, the model is {bias:+.1f} points above (+) or below (-) your "
                 "rating on average; with only a few entries per period, read the rows as a hint, not as a result")
    return "\n".join(lines)


def build_report(con: sqlite3.Connection, by: str, min_n: int, date_from: date | None = None,
                 date_to: date | None = None, notes=()) -> tuple[str, list[Period]]:
    rows, dated = load(con, by, date_from, date_to)
    if not rows:
        return ("No mood values yet. Rate some days with 'Stimmung = +2' and import, and/or run: "
                ".\\enrich.ps1 --mood-only"), []
    periods = summarize(rows)
    rated = sum(s == "rated" for _, _, s in rows)
    head = (f"Mood by {by}: {len(rows)} of {dated} dated entries have a mood "
            f"({rated} rated by you, {len(rows) - rated} estimated by the model)")
    check = render_check(rated_check(con, by, date_from, date_to))
    adjusted = render_adjusted(adjusted_estimates(con, by, date_from, date_to))
    if check and adjusted:
        check += "\n\n" + adjusted
    marks, note_lines = note_marks(periods, by, notes)
    parts = [head, render_table(periods, min_n, marks) + ("\n" + "\n".join(note_lines) if note_lines else ""),
             "\n".join(conclusions(periods, min_n))]
    if check:
        parts.append(check)
    parts.append("Please note:\n" + "\n".join(f"- {c}" for c in CAVEATS))
    return "\n\n".join(parts), periods


def main(argv: list[str] | None = None) -> None:
    from search.ask import parse_when

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DATA / "corpus.db")
    ap.add_argument("--by", choices=BANDS, default="year")
    ap.add_argument("--from", dest="date_from", help="YYYY, YYYY-MM or YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to")
    ap.add_argument("--min-n", type=int, default=10, help="periods with fewer entries are not interpreted")
    ap.add_argument("--chart", type=Path, help="where to write the PNG (default: data/mood_by_<by>.png)")
    ap.add_argument("--no-chart", action="store_true")
    ap.add_argument("--notes", type=Path, default=NOTES, help="period notes (see enrich.notes)")
    ap.add_argument("--no-notes", action="store_true", help="ignore the period notes file (and its chart_from)")
    ap.add_argument("--chart-from", type=int, metavar="YEAR",
                    help="first year of the chart (default: chart_from in the notes file, else all years)")
    args = ap.parse_args(argv)
    if not args.db.exists():
        raise SystemExit(f"{args.db} not found. Open the vault and run the import first.")
    try:
        notes = [] if args.no_notes else load_notes(args.notes)
        chart_from = args.chart_from if args.chart_from is not None else (
            None if args.no_notes else load_chart_from(args.notes))
    except ValueError as e:
        raise SystemExit(f"{e}\nFix the notes file or run with --no-notes.")
    con = connect(args.db)
    text, periods = build_report(con, args.by, args.min_n, parse_when(args.date_from),
                                 parse_when(args.date_to, end=True), notes)
    print(text)
    shown = from_year(periods, chart_from)
    if periods and not shown and not args.no_chart:
        print(f"\nNo chart: no period starts in {chart_from} or later.")
    if shown and not args.no_chart:
        path = args.chart or DATA / f"mood_by_{args.by}.png"
        try:
            written = render_chart(shown, args.by, args.min_n, path, notes)
        except OSError as e:
            raise SystemExit(f"\nThe table above is complete, but the chart could not be written to {path.parent}: {e}\n"
                             "Check that the folder exists and is writable, or give another place with --chart.")
        print(f"\nChart written to {written}")
        if written != path:
            print(f"({path.name} could not be replaced, probably because it is open in a viewer; "
                  "close it to use the old name again.)")


if __name__ == "__main__":
    main()
