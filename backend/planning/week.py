"""Weekly report of a daily plan file: steps per goal, measured against the goals file.

    python -m planning.week                         # the current week, with the last 4 weeks as a trend
    python -m planning.week --date 2027-01-15       # the week that contains this day
    python -m planning.week --weeks 8 --no-notes    # longer trend, without the written notes

The plan file (data/planning/tagesplan.md, in the vault) has one section per day. Lines "Key = value"
are measurements, lines "Key: text" are notes; everything else (the plan itself) is ignored:

    ## 2026-11-03
    Plan:
    - G1: interval session (45 min)
    Balance:
    G1 = 45
    G2 = yes
    Mood = +1
    Lesson: <one sentence>

The goals file (data/planning/goals.yaml) says what each key means (config/goals.example.yaml).
Numbers come from your lines only; nothing is estimated.
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

DATA = Path(__file__).resolve().parents[2] / "data"
PLAN = DATA / "planning" / "tagesplan.md"
GOALS = DATA / "planning" / "goals.yaml"

KINDS = ("hours", "minutes", "count", "yesno", "scale")
HEADING = re.compile(r"^#{1,3}\s*(?:(\d{4})-(\d{2})-(\d{2})|(\d{1,2})\.(\d{1,2})\.(\d{4}))\b")
VALUE_LINE = re.compile(r"^\s*(?P<key>[^\W\d_][\w-]*)\s*=\s*(?P<value>\S.*?)\s*$")
NOTE_LINE = re.compile(r"^\s*(?P<key>[^\W\d_][\w-]*)\s*:\s*(?P<text>\S.*?)\s*$")
YES, NO = {"ja", "j", "yes", "y", "x", "1"}, {"nein", "n", "no", "0", "-"}


@dataclass(frozen=True)
class Metric:
    key: str
    title: str
    kind: str
    weekly_min: float | None = None
    daily_max: tuple[tuple[date, float], ...] = ()   # (until, max): the first stage not yet passed applies

    def limit_on(self, day: date) -> tuple[date, float] | None:
        return next((s for s in self.daily_max if day <= s[0]), None)


@dataclass(frozen=True)
class Milestone:
    day: date
    goal: str
    title: str
    done: bool = False


@dataclass
class Goals:
    metrics: dict[str, Metric]
    notes: tuple[str, ...]
    milestones: list[Milestone]


@dataclass
class Day:
    day: date
    values: dict[str, float] = field(default_factory=dict)
    notes: list[tuple[str, str]] = field(default_factory=list)


def _date(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def load_goals(path: str | Path) -> Goals:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    metrics = {}
    for key, spec in (cfg.get("metrics") or {}).items():
        kind = spec.get("kind")
        if kind not in KINDS:
            raise SystemExit(f"{path}: metric {key}: kind must be one of {', '.join(KINDS)}")
        stages = tuple(sorted((_date(s["until"]), float(s["max"])) for s in spec.get("daily_max") or []))
        metrics[str(key)] = Metric(str(key), spec.get("title", str(key)), kind,
                                   float(spec["weekly_min"]) if "weekly_min" in spec else None, stages)
    milestones = sorted((Milestone(_date(m["date"]), str(m.get("goal", "")), m.get("title", ""), bool(m.get("done")))
                         for m in cfg.get("milestones") or []), key=lambda m: m.day)
    return Goals(metrics, tuple(str(n) for n in cfg.get("notes") or ()), milestones)


def parse_value(kind: str, raw: str) -> float | None:
    s = raw.strip().lower().replace(",", ".").replace("−", "-").replace("–", "-")
    if kind == "yesno":
        return 1.0 if s in YES else 0.0 if s in NO else None
    s = re.sub(r"\s*(?:h|min|std)\.?$", "", s)
    try:
        v = float(s)
    except ValueError:
        return None
    if kind == "scale":
        return v if -5 <= v <= 5 else None
    return v if v >= 0 and (kind != "count" or v.is_integer()) else None


def parse_plan(text: str, goals: Goals) -> tuple[list[Day], list[str]]:
    """Days in the file, and problems as "line N: ..." (keys and line numbers only, never the text)."""
    days: dict[date, Day] = {}
    current: Day | None = None
    problems: list[str] = []
    for n, line in enumerate(text.split("\n"), 1):
        if h := HEADING.match(line):
            y, mo, d = (h[1], h[2], h[3]) if h[1] else (h[6], h[5], h[4])
            try:
                current = days.setdefault(date(int(y), int(mo), int(d)), Day(date(int(y), int(mo), int(d))))
            except ValueError:
                problems.append(f"line {n}: not a valid date")
                current = None
            continue
        if current is None:
            continue
        if (m := VALUE_LINE.match(line)) and m["key"] in goals.metrics:
            v = parse_value(goals.metrics[m["key"]].kind, m["value"])
            if v is None:
                problems.append(f"line {n}: value of {m['key']} not understood")
            else:
                current.values[m["key"]] = v
        elif (m := NOTE_LINE.match(line)) and m["key"] in goals.notes:
            current.notes.append((m["key"], m["text"]))
    return sorted(days.values(), key=lambda d: d.day), problems


def week_of(day: date) -> tuple[date, date]:
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def _num(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v))


def summary(metric: Metric, values: list[float]) -> float | None:
    """One number per week: the mean for hours and scale, the sum for minutes and counts, yes-days for yes/no."""
    if not values:
        return None
    return sum(values) / len(values) if metric.kind in ("hours", "scale") else sum(values)


def metric_line(metric: Metric, values: list[float], week_end: date) -> str:
    head = f"  {metric.key:<10} {metric.title}: "
    if not values:
        return head + "no value this week"
    s = summary(metric, values)
    n = len(values)
    if metric.kind == "hours":
        text = f"mean {_num(s)} h per day ({n} days)"
    elif metric.kind == "scale":
        text = f"mean {s:+.1f} ({n} days)"
    elif metric.kind == "minutes":
        text = f"{_num(s)} min in all ({n} days)"
    elif metric.kind == "count":
        text = f"{_num(s)} in all, {_num(round(s / n, 1))} per day ({n} days)"
    else:
        text = f"yes on {_num(s)} of {n} days"
    if (stage := metric.limit_on(week_end)) is not None:
        until, limit = stage
        text += f"; target at most {_num(limit)} per day until {until:%d.%m.%Y}: " + (
            "met" if s <= limit else f"{_num(round(s - limit, 1))} over")
    if metric.weekly_min is not None:
        text += f"; target at least {_num(metric.weekly_min)} per week: " + (
            "met" if s >= metric.weekly_min else f"{_num(metric.weekly_min - s)} short")
    return head + text


def report(days: list[Day], goals: Goals, today: date, weeks: int = 4, notes: bool = True) -> str:
    start, end = week_of(today)
    last = min(end, today)
    by_day = {d.day: d for d in days}
    in_week = [by_day[d] for d in (start + timedelta(i) for i in range(7)) if d in by_day and d <= last]
    with_values = [d for d in in_week if d.values]
    elapsed = [start + timedelta(i) for i in range((last - start).days + 1)]
    missing = [d for d in elapsed if d not in {x.day for x in with_values}]
    iso = start.isocalendar()
    out = [f"Week {iso.year}-W{iso.week:02d} ({start:%d.%m.%Y} .. {end:%d.%m.%Y}): "
           f"{len(with_values)} of {len(elapsed)} days with a balance"
           + (f"; missing: {', '.join(f'{d:%d.%m.}' for d in missing)}" if missing else "")]
    for metric in goals.metrics.values():
        out.append(metric_line(metric, [d.values[metric.key] for d in with_values if metric.key in d.values], end))

    if weeks > 1:
        starts = [start - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
        out += ["", f"Last {weeks} weeks (mean for hours and scales, sum for minutes and counts, yes-days):",
                "  " + f"{'':<10}" + "".join(f"{f'W{s.isocalendar().week:02d}':>7}" for s in starts)]
        for metric in goals.metrics.values():
            cells = []
            for s in starts:
                vals = [by_day[d].values[metric.key] for d in (s + timedelta(i) for i in range(7))
                        if d in by_day and d <= today and metric.key in by_day[d].values]
                v = summary(metric, vals)
                cells.append(f"{'-' if v is None else _num(round(v, 1)):>7}")
            out.append(f"  {metric.key:<10}" + "".join(cells))

    overdue = [m for m in goals.milestones if not m.done and m.day < today]
    upcoming = [m for m in goals.milestones if not m.done and today <= m.day <= today + timedelta(days=31)]
    if overdue or upcoming:
        out += ["", "Milestones:"]
        out += [f"  OVERDUE  {m.day:%d.%m.%Y} {m.goal} {m.title}" for m in overdue]
        out += [f"  due      {m.day:%d.%m.%Y} {m.goal} {m.title}" for m in upcoming]

    if notes and any(d.notes for d in in_week):
        out += ["", "Notes of the week (private, for this screen only):"]
        out += [f"  {d.day:%d.%m.} {key}: {text}" for d in in_week for key, text in d.notes]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", type=Path, default=PLAN)
    ap.add_argument("--goals", type=Path, default=GOALS)
    ap.add_argument("--date", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(), default=None,
                    help="a day of the week to report (default: today)")
    ap.add_argument("--weeks", type=int, default=4, help="weeks in the trend table (1 = none)")
    ap.add_argument("--no-notes", action="store_true", help="leave out the written notes (to show the report)")
    args = ap.parse_args(argv)
    for path in (args.plan, args.goals):
        if not path.exists():
            raise SystemExit(f"{path} not found. Is the vault open? (scripts\\vault-open.ps1)")
    goals = load_goals(args.goals)
    days, problems = parse_plan(args.plan.read_text(encoding="utf-8"), goals)
    print(report(days, goals, args.date or date.today(), args.weeks, notes=not args.no_notes))
    if problems:
        print("\nLines not read: " + "; ".join(problems))


if __name__ == "__main__":
    main()
