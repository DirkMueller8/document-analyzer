"""Statistics on a daily measurement series (default: body weight).

    python -m analysis.weight                 # summary, yearly table, recent months, jumps
    python -m analysis.weight --csv out.csv   # daily series for your own charts (keep it in data/!)

The output is health data: it stays on this machine unless you decide otherwise.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import statistics as st
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "data"


@dataclass(frozen=True)
class Point:
    day: date
    value: float


def load_series(con: sqlite3.Connection, kind: str = "weight") -> list[Point]:
    """One value per day: values written on the day itself win over later references; ties are averaged."""
    by_day: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for d, v, source in con.execute("SELECT date, value, source FROM measurements WHERE kind = ?", (kind,)):
        by_day[d][source].append(v)
    series = []
    for d, sources in by_day.items():
        values = sources.get("entry") or sources["ref"]
        series.append(Point(date.fromisoformat(d), round(st.fmean(values), 2)))
    return sorted(series, key=lambda p: p.day)


def value_at_or_before(series: list[Point], day: date, max_gap: int = 31) -> Point | None:
    candidates = [p for p in series if day - timedelta(days=max_gap) <= p.day <= day]
    return candidates[-1] if candidates else None


def rolling_mean(series: list[Point], end: date, days: int) -> float | None:
    window = [p.value for p in series if end - timedelta(days=days) < p.day <= end]
    return round(st.fmean(window), 2) if window else None


def slope_per_year(series: list[Point], min_points: int = 20, min_span: int = 90) -> float | None:
    """Least-squares trend in units per year; None if too few points or too short a span to mean anything."""
    if len(series) < min_points or (series[-1].day - series[0].day).days < min_span:
        return None
    x = [(p.day - series[0].day).days for p in series]
    if len(set(x)) < 2:
        return None
    return round(st.linear_regression(x, [p.value for p in series]).slope * 365.25, 2)


def longest_streak(series: list[Point]) -> tuple[int, date | None, date | None]:
    best, cur_start, prev = 0, None, None
    best_range: tuple[date | None, date | None] = (None, None)
    for p in series:
        cur_start = p.day if prev is None or (p.day - prev).days != 1 else cur_start
        length = (p.day - cur_start).days + 1
        if length > best:
            best, best_range = length, (cur_start, p.day)
        prev = p.day
    return best, *best_range


def outliers(series: list[Point], max_dev: float = 5.0, window: int = 30) -> list[tuple[Point, float]]:
    """Points far from the median of their neighbours (±window days, at least 3 of them): likely typos.

    Returns (point, neighbour median) pairs.
    """
    out = []
    for p in series:
        near = [q.value for q in series if q is not p and abs((q.day - p.day).days) <= window]
        if len(near) >= 3:
            med = st.median(near)
            if abs(p.value - med) > max_dev:
                out.append((p, med))
    return out


def jumps(series: list[Point], threshold: float = 3.0, max_days: int = 3) -> list[tuple[Point, Point]]:
    """Changes of more than `threshold` within `max_days`: often typos (85,5 vs 58,5)."""
    return [(a, b) for a, b in zip(series, series[1:])
            if (b.day - a.day).days <= max_days and abs(b.value - a.value) > threshold]


def summarize(series: list[Point], unit: str = "kg", today: date | None = None) -> str:
    if not series:
        return "No measurements found. Is the pattern in data/measurements.yaml right?"
    today = today or date.today()
    flagged = outliers(series)
    bad = {id(p) for p, _ in flagged}
    series = [p for p in series if id(p) not in bad]  # statistics without likely typos
    if not series:
        return "All measurements look like outliers."
    last = series[-1]
    lo = min(series, key=lambda p: p.value)
    hi = max(series, key=lambda p: p.value)
    span = (last.day - series[0].day).days + 1
    streak, s_from, s_to = longest_streak(series)

    def change(days: int) -> str:
        ref = value_at_or_before(series, last.day - timedelta(days=days))
        return f"{last.value - ref.value:+.1f} {unit}" if ref else "n/a"

    year_ago = [p for p in series if p.day > last.day - timedelta(days=365)]
    lines = [
        f"Measurements:      {len(series)} days from {series[0].day} to {last.day}"
        f" ({len(series) / span:.0%} of days covered)",
        f"Latest:            {last.value:.1f} {unit} on {last.day}",
        f"Lowest / highest:  {lo.value:.1f} {unit} ({lo.day}) / {hi.value:.1f} {unit} ({hi.day})",
        f"Mean (all):        {st.fmean(p.value for p in series):.1f} {unit}",
        f"7 / 30 / 90-day mean: " + " / ".join(
            f"{m:.1f}" if (m := rolling_mean(series, last.day, d)) is not None else "n/a" for d in (7, 30, 90)),
        f"Change 30 / 90 / 365 days: {change(30)} / {change(90)} / {change(365)}",
        f"Trend last 12 months: {s:+.1f} {unit}/year" if (s := slope_per_year(year_ago)) is not None
        else "Trend last 12 months: n/a",
        f"Longest daily streak: {streak} days ({s_from} .. {s_to})",
        "",
        f"{'Year':<6}{'days':>6}{'mean':>8}{'min':>8}{'max':>8}{'trend/yr':>10}",
    ]
    by_year: dict[int, list[Point]] = defaultdict(list)
    for p in series:
        by_year[p.day.year].append(p)
    for y, pts in sorted(by_year.items()):
        vals = [p.value for p in pts]
        tr = slope_per_year(pts)
        lines.append(f"{y:<6}{len(pts):>6}{st.fmean(vals):>8.1f}{min(vals):>8.1f}{max(vals):>8.1f}"
                     f"{(f'{tr:+.1f}' if tr is not None else '-'):>10}")

    by_month: dict[str, list[float]] = defaultdict(list)
    for p in series:
        by_month[p.day.strftime("%Y-%m")].append(p.value)
    recent = sorted(by_month.items())[-12:]
    lines += ["", "Last 12 months with data (mean):"]
    lines += [f"  {m}: {st.fmean(v):.1f} {unit}  ({len(v)} days)" for m, v in recent]

    if flagged:
        lines += ["", f"Likely typos, excluded from the statistics above (> 5 {unit} from the median"
                      f" of the surrounding 30 days) - {len(flagged)}:"]
        lines += [f"  {p.day}: {p.value:.1f}  (neighbours around {med:.1f})" for p, med in flagged[:30]]
    j = jumps(series)
    if j:
        lines += ["", f"Remaining jumps (> 3 {unit} within 3 days) - {len(j)}:"]
        lines += [f"  {a.day} {a.value:.1f} -> {b.day} {b.value:.1f}" for a, b in j[:20]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DATA / "corpus.db")
    ap.add_argument("--kind", default="weight")
    ap.add_argument("--csv", type=Path, help="write the daily series to this CSV file")
    args = ap.parse_args(argv)
    if not args.db.exists():
        raise SystemExit(f"{args.db} not found. Open the vault and run the import first.")

    con = sqlite3.connect(str(args.db))
    series = load_series(con, args.kind)
    unit = (con.execute("SELECT unit FROM measurements WHERE kind = ? LIMIT 1", (args.kind,)).fetchone() or ["kg"])[0]
    print(summarize(series, unit or ""))
    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["date", args.kind])
            w.writerows([p.day.isoformat(), str(p.value).replace(".", ",")] for p in series)
        print(f"\nWrote {len(series)} rows to {args.csv}")


if __name__ == "__main__":
    main()
