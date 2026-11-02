"""Measure extraction accuracy against a small sample labeled by hand.

    python -m enrich.evaluate sample --n 30                 # writes data/enrich_labels.yaml (vault)
    python -m enrich.evaluate sample --n 30 --from 2008 --to 2010
    python -m enrich.evaluate score                         # qwen3:14b, no thinking
    python -m enrich.evaluate score --model qwen3:30b-a3b --think
    python -m enrich.evaluate score --details            # + differences per entry (private, screen only)
    python -m enrich.evaluate mood                       # model vs the moods you rated ("Stimmung = +2")
    python -m enrich.evaluate calibrate                  # would a simple stretch of the model's scale help?
    python -m enrich.evaluate achievements --details     # model vs the entries you marked ("Achievement = 2")
    python -m enrich.evaluate achievements --reviewed-from 2012 --reviewed-to 2012 --negatives 60
    python -m enrich.evaluate achievements --set validation   # once, at the very end

A fresh sample for validation (entries already in other enrich_labels*.yaml files are skipped):
    python -m enrich.evaluate sample --n 30 --labels data/enrich_labels_validation.yaml

`sample` picks random entries and writes a label file INSIDE the vault. It contains the entry
text so you can label without searching the diary, plus empty fields to fill in; set `done: true`
on each entry you have checked. An existing label file is never overwritten.

`score` runs the extraction on the labeled entries with the chosen model (nothing is stored in
the database) and prints accuracy figures only - no text, no names.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ingest.store import connect

from .calibrate import calibrate, load_pairs, save_pairs
from .extract import extract_entry, extract_mood, system_prompt
from .notes import DEFAULT as NOTES, context_for, load_notes
from .run import mood_version, version
from .schema import build_model
from .taxonomy import Domain, load_domains

DATA = Path(__file__).resolve().parents[2] / "data"
LABELS = DATA / "enrich_labels.yaml"
PAIRS = DATA / "mood_pairs.json"
PAIRS_MOOD_ONLY = DATA / "mood_pairs_mood_only.json"
ACHIEVEMENT_RUNS = DATA / "achievement_validation_runs.json"  # extraction versions that already saw the validation set
KINDS = ("first", "achievement", "setback", "decision")
SHORT_CHARS = 1500  # about half a page; longer entries often mix several moods


@dataclass
class Label:
    """What is true for one entry, by hand (or, for a prediction, by the model)."""
    mood: int | None = None
    people: set[str] = field(default_factory=set)
    domains: set[str] = field(default_factory=set)
    health: bool = False
    milestones: set[str] = field(default_factory=set)
    # predictions only, for the breakdowns: categories of the health events, names not kept and why
    health_categories: set[str] = field(default_factory=set)
    dropped: dict[str, str] = field(default_factory=dict)


def _items(value) -> list[str]:
    """A YAML list, or a plain "a, b" string (written without brackets)."""
    if value is None:
        return []
    if isinstance(value, str):
        value = value.split(",")
    return [str(v).strip() for v in value if str(v).strip()]


def _norm(names) -> set[str]:
    return {n.casefold() for n in _items(names)}


def label_from_yaml(item: dict) -> Label:
    mood = item.get("mood")
    return Label(None if mood in (None, "") else int(mood), _norm(item.get("people")),
                 set(_items(item.get("domains"))), bool(item.get("health")), set(_items(item.get("milestones"))))


def label_from_result(result, checks=None) -> Label:
    return Label(result.mood_score, _norm(result.people), {a.domain for a in result.activities},
                 bool(result.health_events), {m.kind for m in result.milestones},
                 {h.category for h in result.health_events}, dict(checks.dropped_names) if checks else {})


MISS_REASONS = {"public_or_watched": "marked public or only watched", "not_a_person": "marked not a person",
                "not_in_text": "removed by the text check"}


def is_variant(a: str, b: str) -> bool:
    """Same person written differently: every word of the shorter name starts a word of the longer one
    ("lena" ~ "lena berg", "lena b." ~ "lena berg"). Such variants are merged later with people.ps1."""
    ta, tb = re.findall(r"\w+", a), re.findall(r"\w+", b)
    short, long = sorted((ta, tb), key=len)
    return bool(short) and all(any(w.startswith(s) for w in long) for s in short)


def match_people(truth: set[str], pred: set[str]) -> tuple[int, int, set[str], set[str]]:
    """(exact matches, variant matches, unmatched true names, unmatched predicted names)."""
    exact = truth & pred
    t_left, p_left = sorted(truth - exact), set(pred - exact)
    variants = 0
    for t in list(t_left):
        p = next((p for p in sorted(p_left) if is_variant(t, p)), None)
        if p is not None:
            variants += 1
            t_left.remove(t)
            p_left.discard(p)
    return len(exact), variants, set(t_left), p_left


def miss_reason(name: str, pred: Label) -> str:
    """Why the model did not deliver a name you labeled (casefolded names)."""
    if name in pred.dropped:
        return MISS_REASONS[pred.dropped[name]]
    similar = lambda p: is_variant(name, p) or name in p or p in name  # noqa: E731
    if dropped := next((p for p in sorted(pred.dropped) if similar(p)), None):
        return "a variant was " + MISS_REASONS[pred.dropped[dropped]]
    if any(similar(p) for p in pred.people):
        return "variant of a name already matched to another person"
    return "not found"


def _tally(d: dict, key) -> None:
    d[key] = d.get(key, 0) + 1


def _list(d: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(d.items(), key=lambda kv: -kv[1])) or "none"


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    def add(self, truth: set, pred: set) -> None:
        self.tp += len(truth & pred)
        self.fp += len(pred - truth)
        self.fn += len(truth - pred)

    def __str__(self) -> str:
        p = self.tp / (self.tp + self.fp) if self.tp + self.fp else None
        r = self.tp / (self.tp + self.fn) if self.tp + self.fn else None
        f1 = 2 * p * r / (p + r) if p and r else None
        pct = lambda x: "  -  " if x is None else f"{100 * x:3.0f} %"  # noqa: E731
        return (f"precision {pct(p)}  recall {pct(r)}  F1 {pct(f1)}  "
                f"({self.tp} right, {self.fp} extra, {self.fn} missed)")


@dataclass
class Score:
    entries: int = 0
    mood_both: int = 0
    mood_abs_error: int = 0
    mood_sign_agree: int = 0
    mood_missed: int = 0     # you gave a mood, the model none
    mood_extra: int = 0      # the model gave a mood, you none
    mood_bias: int = 0       # sum of (model - you): > 0 means the model rates too positive
    mood_signs: dict = field(default_factory=dict)  # (your sign, model sign) -> count
    mood_by_rated: dict = field(default_factory=dict)  # your value -> [entries, sum of the model's values]
    people: Counts = field(default_factory=Counts)
    domains: Counts = field(default_factory=Counts)
    health: Counts = field(default_factory=Counts)
    milestones: Counts = field(default_factory=Counts)
    mood_by_length: dict = field(default_factory=dict)     # "short"/"long" -> [entries, abs error, same sign]
    people_variants: int = 0                               # counted right: same person, other spelling
    people_missed: dict = field(default_factory=dict)      # reason -> count
    per_domain: dict = field(default_factory=dict)         # domain -> Counts
    health_extra: dict = field(default_factory=dict)       # model category on entries you left without -> count
    milestone_entries: dict = field(default_factory=dict)  # how you and the model mark entries -> count

    def add(self, truth: Label, pred: Label, length: int = 0) -> None:
        self.entries += 1
        if truth.mood is not None and pred.mood is not None:
            bucket = self.mood_by_length.setdefault("short" if length <= SHORT_CHARS else "long", [0, 0, 0])
            bucket[0] += 1
            bucket[1] += abs(truth.mood - pred.mood)
            bucket[2] += (truth.mood > 0) - (truth.mood < 0) == (pred.mood > 0) - (pred.mood < 0)
        exact, variants, missed, extra = match_people(truth.people, pred.people)
        self.people.tp += exact + variants
        self.people.fp += len(extra)
        self.people.fn += len(missed)
        self.people_variants += variants
        for name in missed:
            _tally(self.people_missed, miss_reason(name, pred))
        for d in truth.domains | pred.domains:
            self.per_domain.setdefault(d, Counts()).add(truth.domains & {d}, pred.domains & {d})
        if pred.health and not truth.health:
            for c in pred.health_categories:
                _tally(self.health_extra, c)
        if truth.milestones or pred.milestones:
            _tally(self.milestone_entries,
                   "only you" if not pred.milestones else "only the model" if not truth.milestones
                   else "both, a kind in common" if truth.milestones & pred.milestones else "both, different kinds")
        if truth.mood is not None and pred.mood is not None:
            self.mood_both += 1
            self.mood_abs_error += abs(truth.mood - pred.mood)
            sign = lambda v: (v > 0) - (v < 0)  # noqa: E731
            self.mood_sign_agree += sign(truth.mood) == sign(pred.mood)
            self.mood_bias += pred.mood - truth.mood
            key = (sign(truth.mood), sign(pred.mood))
            self.mood_signs[key] = self.mood_signs.get(key, 0) + 1
            by_value = self.mood_by_rated.setdefault(truth.mood, [0, 0])
            by_value[0] += 1
            by_value[1] += pred.mood
        elif truth.mood is not None:
            self.mood_missed += 1
        elif pred.mood is not None:
            self.mood_extra += 1
        self.domains.add(truth.domains, pred.domains)
        self.health.add({True} if truth.health else set(), {True} if pred.health else set())
        self.milestones.add(truth.milestones, pred.milestones)

    def sign_table(self) -> list[str]:
        names = {-1: "negative", 0: "zero", 1: "positive"}
        rows = ["             model: negative  zero  positive"]
        for t in (-1, 0, 1):
            cells = "".join(f"{self.mood_signs.get((t, p), 0):>{w}}" for p, w in ((-1, 10), (0, 6), (1, 10)))
            rows.append(f"  you {names[t]:>9}:{cells}")
        return rows

    def baseline_lines(self) -> list[str]:
        """What guessing without a model would achieve, and where the model's scale differs from yours."""
        names = {-1: "negative", 0: "zero", 1: "positive"}
        totals = {t: sum(n for (a, _), n in self.mood_signs.items() if a == t) for t in (-1, 0, 1)}
        top = max(totals, key=totals.get)
        return [f"  without a model: always guessing '{names[top]}' (your most frequent sign) would be right in "
                f"{100 * totals[top] / self.mood_both:.0f} %",
                *(f"  you rated {f'{v:+d}' if v else '0'}: {n} entr{'y' if n == 1 else 'ies'}, "
                  f"model average {total / n:+.1f}" for v, (n, total) in sorted(self.mood_by_rated.items()))]

    def mood_lines(self) -> list[str]:
        mood = (f"mean error {self.mood_abs_error / self.mood_both:.1f} points, same sign "
                f"{100 * self.mood_sign_agree / self.mood_both:.0f} % ({self.mood_both} entries), "
                f"model {self.mood_bias / self.mood_both:+.1f} points on average"
                if self.mood_both else "no entry with a mood from both")
        return [
            f"mood:        {mood}; missed {self.mood_missed}, extra {self.mood_extra}",
            *(self.sign_table() if self.mood_both else []),
            *(self.baseline_lines() if self.mood_both else []),
            *(f"  {k} entries ({'up to' if k == 'short' else 'over'} {SHORT_CHARS} characters): {n}, "
              f"mean error {err / n:.1f}, same sign {100 * same / n:.0f} %"
              for k, (n, err, same) in sorted(self.mood_by_length.items(), reverse=True)),
        ]

    def __str__(self) -> str:
        return "\n".join([
            f"entries scored: {self.entries}",
            *self.mood_lines(),
            f"people:      {self.people}",
            f"  {self.people_variants} of the right ones are spelling variants (e.g. first name only)",
            f"  missed names: {_list(self.people_missed)}",
            f"domains:     {self.domains}",
            *(f"  {d}: {c.tp} right, {c.fp} extra, {c.fn} missed" for d, c in sorted(self.per_domain.items())),
            f"health:      {self.health}",
            f"  extra reports by category: {_list(self.health_extra)}",
            f"milestones:  {self.milestones}",
            f"  entries with milestones: {_list(self.milestone_entries)}",
        ])


class _Literal(str):
    pass


def _dump(items: list[dict], domains: list[Domain]) -> str:
    dumper = type("D", (yaml.SafeDumper,), {})
    dumper.add_representer(_Literal, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|"))
    header = (
        "# Labels for the extraction accuracy check. PRIVATE: lives in the vault, never in git.\n"
        "# For each entry fill in what is TRUE, then set done: true. Only done entries are scored.\n"
        "#   mood:       -5 (very bad) .. +5 (very good); leave empty if the entry says nothing about it\n"
        "#   people:     names of people in your own life, as written (no public figures, teams, places)\n"
        f"#   domains:    activity domains that occur: {', '.join(d.name for d in domains)}\n"
        "#   health:     true if the entry mentions an injury, illness, pain or mental-health event\n"
        f"#   milestones: kinds that occur: {', '.join(KINDS)}\n"
        "# The text field is for reading only and is ignored.\n\n")
    return header + yaml.dump(items, Dumper=dumper, allow_unicode=True, sort_keys=False, width=110)


def repo_path(path: Path) -> Path:
    """A relative path is taken from the repo root (the .ps1 shortcuts run inside backend/)."""
    return path if path.is_absolute() else DATA.parent / path


def labeled_ids(labels_dir: Path, current: Path) -> set[int]:
    """Entry ids in every other label file next to `current` (enrich_labels*.yaml): a new sample skips them."""
    return {it["id"] for it in labeled_items(labels_dir, current)}


def labeled_items(labels_dir: Path, current: Path) -> list[dict]:
    items: list[dict] = []
    for path in sorted(labels_dir.glob("enrich_labels*.yaml")):
        if path.resolve() != current.resolve():
            items += [it for it in load_labels(path) if isinstance(it, dict) and "id" in it]
    return items


def exclusion(con, items: list[dict]) -> tuple[set[int], int, int]:
    """Entries a fresh sample must not contain: (ids, labeled entries edited since, extra entries by date).

    An entry edited after labeling got a new id at the import, so its id is no longer in the label file.
    Everything with the same date as such an entry is excluded too, so its corrected version cannot slip in.
    """
    ids = {it["id"] for it in items}
    stale_dates: set[str] = set()
    stale = 0
    for it in items:
        row = con.execute("SELECT text_hash FROM entries WHERE id = ?", (it["id"],)).fetchone()
        if row is None or not row[0].startswith(str(it.get("hash", ""))):
            stale += 1
            if it.get("date"):
                stale_dates.add(str(it["date"]))
    by_date: set[int] = set()
    if stale_dates:
        marks = ",".join("?" * len(stale_dates))
        by_date = {r[0] for r in con.execute(f"SELECT id FROM entries WHERE raw_date IN ({marks})",
                                             sorted(stale_dates))} - ids
    return ids | by_date, stale, len(by_date)


def sample(con, n: int, seed: int, date_from=None, date_to=None, domains: list[Domain] | None = None,
           exclude: set[int] | frozenset[int] = frozenset()) -> str:
    sql, params = "SELECT id, text_hash, raw_date, text FROM entries WHERE 1=1", []
    if date_from:
        sql += " AND date_start >= ?"
        params.append(date_from.isoformat())
    if date_to:
        sql += " AND date_start <= ?"
        params.append(date_to.isoformat())
    rows = [r for r in con.execute(sql + " ORDER BY id", params).fetchall() if r[0] not in exclude]
    picked = sorted(random.Random(seed).sample(rows, min(n, len(rows))), key=lambda r: r[0])
    items = [{"id": i, "hash": h[:12], "date": raw or "", "done": False, "mood": None, "people": [],
              "domains": [], "health": False, "milestones": [], "text": _Literal(text.rstrip() + "\n")}
             for i, h, raw, text in picked]
    return _dump(items, domains or [])


def load_labels(path: Path) -> list[dict]:
    items = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(items, list):
        raise ValueError(f"{path.name}: expected a list of entries")
    return items


def differences(item: dict, truth: Label, pred: Label, extraction) -> list[str]:
    """Per-entry differences between your labels and the model, for the screen only (private content)."""
    lines = []
    if truth.mood is not None and truth.mood != pred.mood:
        lines.append(f"    mood: you {truth.mood:+d}, model {pred.mood:+d}  ({extraction.mood_reason})")
    _, _, missed, extra = match_people(truth.people, pred.people)
    if missed or extra:
        why = "; ".join(f"{n}: {miss_reason(n, pred)}" for n in sorted(missed))
        lines.append(f"    people: model extra {sorted(extra) or '-'}, missed {sorted(missed) or '-'}"
                     + (f"  [{why}]" if why else ""))
    if truth.domains != pred.domains:
        acts = ", ".join(f"{a.domain}/{a.activity}" for a in extraction.activities if a.domain not in truth.domains)
        lines.append(f"    domains: model extra {sorted(pred.domains - truth.domains) or '-'}"
                     + (f" ({acts})" if acts else "") + f", missed {sorted(truth.domains - pred.domains) or '-'}")
    if truth.health != pred.health:
        found = ", ".join(f"{h.condition} ({h.category})" for h in extraction.health_events)
        lines.append(f"    health: you {'yes' if truth.health else 'no'}, model "
                     f"{'yes: ' + found if pred.health else 'no'}")
    if truth.milestones != pred.milestones:
        titles = ", ".join(f"{m.kind}: {m.title}" for m in extraction.milestones)
        lines.append(f"    milestones: you {sorted(truth.milestones) or '-'}, model {titles or '-'}")
    if not lines:
        return []
    return [f"[{item.get('date') or '-'}] (id {item.get('id')})", *lines]


def score_mood(con, chat, domains: list[Domain], limit: int | None = None, progress=None,
               pairs: list | None = None, mood_only: bool = False, notes=()) -> tuple[Score, dict]:
    """Compare the model's mood with the values you rated yourself ("Stimmung = +2" in the entries).

    The model sees the entries without the marker lines. Nothing is stored. Entries with several rated
    values are skipped (counted), since it is not clear which one belongs to the text.
    """
    model = build_model([d.name for d in domains])
    system = system_prompt(domains)
    rows = con.execute(
        "SELECT e.id, e.raw_date, e.text, COUNT(m.id), MIN(m.value), e.date_start FROM entries e"
        " JOIN measurements m ON m.entry_id = e.id AND m.kind = 'mood' GROUP BY e.id ORDER BY e.id").fetchall()
    result, skipped = Score(), {"several ratings in one entry": 0, "failed": 0}
    todo = [r for r in rows if r[3] == 1]
    skipped["several ratings in one entry"] = len(rows) - len(todo)
    if limit:
        todo = todo[:limit]
    for k, (entry_id, raw_date, text, _, rated, iso) in enumerate(todo, 1):
        try:
            if mood_only:
                predicted = extract_mood(text, raw_date, chat, context=context_for(notes, iso) or None)[0]
            else:
                predicted = extract_entry(text, raw_date, chat, model, system)[0].mood_score
        except ValueError:
            skipped["failed"] += 1
            continue
        result.add(Label(mood=int(rated)), Label(mood=predicted), len(text))
        if pairs is not None:
            pairs.append((entry_id, int(rated), predicted, len(text)))
        if progress:
            progress(k, len(todo))
    return result, skipped


def score(con, items: list[dict], chat, domains: list[Domain], progress=None,
          details: list[str] | None = None) -> tuple[Score, dict]:
    """Extract every done entry again (nothing stored) and compare. Returns (score, counts of skipped).

    With a `details` list, the per-entry differences are appended to it (private content).
    """
    model = build_model([d.name for d in domains])
    system = system_prompt(domains)
    result, skipped = Score(), {"not done": 0, "changed or removed": 0, "failed": 0}
    todo = [it for it in items if it.get("done")]
    skipped["not done"] = len(items) - len(todo)
    for k, item in enumerate(todo, 1):
        row = con.execute("SELECT raw_date, text, text_hash FROM entries WHERE id = ?", (item.get("id"),)).fetchone()
        if row is None or not row[2].startswith(str(item.get("hash", ""))):
            skipped["changed or removed"] += 1
            continue
        try:
            extraction, checks, _ = extract_entry(row[1], row[0], chat, model, system)
        except ValueError:  # includes pydantic's ValidationError
            skipped["failed"] += 1
            continue
        truth, pred = label_from_yaml(item), label_from_result(extraction, checks)
        result.add(truth, pred, len(row[1]))
        if details is not None:
            details += differences(item, truth, pred, extraction)
        if progress:
            progress(k, len(todo))
    return result, skipped


def achievement_set(iso_date: str | None) -> str:
    """Stable split of the marked entries: a third is held back for validation. By date, not by id, so
    editing an entry does not move it to the other set."""
    return "validation" if int(hashlib.sha256((iso_date or "").encode("utf-8")).hexdigest(), 16) % 3 == 0 else "tune"


@dataclass
class AchievementScore:
    """The model's "achievement" milestones against the entries you marked ("Achievement = 2")."""
    by_level: dict = field(default_factory=dict)  # your level -> [entries, found as achievement, only another kind]
    model_total: int = 0                          # achievements the model reported on marked entries
    unmarked: int = 0                             # entries from a period you went through completely
    flagged: int = 0                              # ... where the model still reports an achievement
    flagged_total: int = 0

    def add_marked(self, level: int, kinds: list[str]) -> None:
        row = self.by_level.setdefault(level, [0, 0, 0])
        n = kinds.count("achievement")
        row[0] += 1
        row[1] += n > 0
        row[2] += n == 0 and bool(kinds)
        self.model_total += n

    def add_unmarked(self, kinds: list[str]) -> None:
        n = kinds.count("achievement")
        self.unmarked += 1
        self.flagged += n > 0
        self.flagged_total += n

    @property
    def marked(self) -> int:
        return sum(r[0] for r in self.by_level.values())

    def __str__(self) -> str:
        pct = lambda a, b: f"{100 * a / b:.0f} %" if b else "-"  # noqa: E731
        found = sum(r[1] for r in self.by_level.values())
        lines = [f"marked entries scored: {self.marked}; the model found an achievement in {found} "
                 f"({pct(found, self.marked)})"]
        for level, (n, f, other) in sorted(self.by_level.items()):
            lines.append(f"  marked {level}: {n} entries, found {f} ({pct(f, n)}), only another kind {other}, "
                         f"nothing {n - f - other}")
        if self.marked:
            lines.append(f"  achievements the model reports per marked entry: {self.model_total / self.marked:.1f}")
        lines.append(f"unmarked entries scored: {self.unmarked}" + (
            f"; the model reports an achievement in {self.flagged} ({pct(self.flagged, self.unmarked)}), "
            f"{self.flagged_total} in all" if self.unmarked else
            " (name a period you went through completely: --reviewed-from/--reviewed-to and --negatives N)"))
        return "\n".join(lines)


def score_achievements(con, chat, domains: list[Domain], which: str = "tune", reviewed_from=None, reviewed_to=None,
                       negatives: int = 0, seed: int = 1, progress=None,
                       details: list[str] | None = None) -> tuple[AchievementScore, dict]:
    """Extract the marked entries (and a sample of unmarked ones) again; nothing is stored.

    `which` is "tune", "validation" or "all" (see achievement_set). Unmarked entries only count as "no
    achievement" inside the period you went through completely.
    """
    model = build_model([d.name for d in domains])
    system = system_prompt(domains)
    keep = lambda iso: which == "all" or achievement_set(iso) == which  # noqa: E731
    jobs = [(r[0], r[1], r[2], r[3], int(r[4])) for r in con.execute(
        "SELECT e.id, e.raw_date, e.text, e.date_start, MAX(m.value) FROM entries e"
        " JOIN measurements m ON m.entry_id = e.id AND m.kind = 'achievement' GROUP BY e.id ORDER BY e.id")
        if keep(r[3])]
    if negatives:
        sql = ("SELECT id, raw_date, text, date_start FROM entries WHERE date_precision = 'day' "
               "AND date_start IS NOT NULL AND TRIM(text) != '' "
               "AND id NOT IN (SELECT entry_id FROM measurements WHERE kind = 'achievement')")
        params: list = []
        if reviewed_from:
            sql += " AND date_start >= ?"
            params.append(reviewed_from.isoformat())
        if reviewed_to:
            sql += " AND date_start <= ?"
            params.append(reviewed_to.isoformat())
        rows = [r for r in con.execute(sql + " ORDER BY id", params) if keep(r[3])]
        picked = sorted(random.Random(seed).sample(rows, min(negatives, len(rows))), key=lambda r: r[0])
        jobs += [(*r, 0) for r in picked]
    result, skipped = AchievementScore(), {"failed": 0}
    for k, (entry_id, raw_date, text, iso, level) in enumerate(jobs, 1):
        try:
            extraction = extract_entry(text, raw_date, chat, model, system)[0]
        except ValueError:  # includes pydantic's ValidationError
            skipped["failed"] += 1
            continue
        kinds = [m.kind for m in extraction.milestones]
        titles = ", ".join(f"{m.kind}: {m.title}" for m in extraction.milestones) or "none"
        if level:
            result.add_marked(level, kinds)
            if details is not None and "achievement" not in kinds:
                details.append(f"[{raw_date or '-'}] (id {entry_id}) marked {level}, model milestones: {titles}")
        else:
            result.add_unmarked(kinds)
            if details is not None and "achievement" in kinds:
                details.append(f"[{raw_date or '-'}] (id {entry_id}) not marked, model milestones: {titles}")
        if progress:
            progress(k, len(jobs))
    return result, skipped


def main(argv: list[str] | None = None) -> None:
    from search.ask import parse_when
    from search.ollama_client import CHAT_MODEL, OllamaError, chat_json, require

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample", help="write a label file with N random entries")
    s.add_argument("--n", type=int, default=30)
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--from", dest="date_from")
    s.add_argument("--to", dest="date_to")
    c = sub.add_parser("score", help="extract the labeled entries again and compare")
    c.add_argument("--model", default=CHAT_MODEL)
    c.add_argument("--think", action="store_true")
    c.add_argument("--details", action="store_true",
                   help="also list the differences per entry (names, conditions - private, for your screen only)")
    m = sub.add_parser("mood", help="compare the model's mood with the values you rated (Stimmung = +2)")
    m.add_argument("--model", default=CHAT_MODEL)
    m.add_argument("--limit", type=int, help="only the first N rated entries")
    m.add_argument("--notes", type=Path, default=NOTES, help="period notes with optional context (mood-only variant)")
    m.add_argument("--no-context", action="store_true", help="rate without the background of the period notes")
    k = sub.add_parser("calibrate", help="fit your rating ~ model and check it by leave-one-out (needs 'mood' first)")
    for p in (m, k):
        p.add_argument("--pairs", type=Path, default=PAIRS, help="numbers only: your rating and the model's value")
        p.add_argument("--mood-only", action="store_true",
                       help="the faster mood-only variant (its pairs are kept in a separate file)")
    a = sub.add_parser("achievements", help="compare the model's achievements with the entries you marked")
    a.add_argument("--model", default=CHAT_MODEL)
    a.add_argument("--think", action="store_true")
    a.add_argument("--set", dest="which", choices=("tune", "validation", "all"), default="tune",
                   help="tune (about two thirds of the marked entries, default), validation (the rest, once at the "
                        "end) or all")
    a.add_argument("--again", action="store_true", help="score the validation set again with the same version")
    a.add_argument("--reviewed-from", help="start of a period you marked completely (YYYY, YYYY-MM, YYYY-MM-DD)")
    a.add_argument("--reviewed-to", help="end of that period")
    a.add_argument("--negatives", type=int, default=0, help="also score N unmarked entries of that period")
    a.add_argument("--seed", type=int, default=1)
    a.add_argument("--details", action="store_true",
                   help="list missed and unmarked-but-reported entries (diary content - for your screen only)")
    for p in (s, c, m, k, a):
        p.add_argument("--db", type=Path, default=DATA / "corpus.db")
        p.add_argument("--labels", type=Path, default=LABELS)
        p.add_argument("--taxonomy", type=Path)
    args = ap.parse_args(argv)
    args.db, args.labels = repo_path(args.db), repo_path(args.labels)
    args.taxonomy = repo_path(args.taxonomy) if args.taxonomy else None
    if args.cmd in ("mood", "calibrate"):
        if args.mood_only and args.pairs == PAIRS:
            args.pairs = PAIRS_MOOD_ONLY
        args.pairs = repo_path(args.pairs)
    if args.cmd == "calibrate":
        if not args.pairs.exists():
            raise SystemExit(f"{args.pairs} not found. Run first: python -m enrich.evaluate mood")
        info, pairs = load_pairs(args.pairs)
        try:
            print(f"Pairs from the run with {info['model']}, extraction version {info['version']}")
            print(calibrate(pairs))
        except ValueError as e:
            raise SystemExit(str(e))
        return
    if not args.db.exists():
        raise SystemExit(f"{args.db} not found. Open the vault and run the import first.")
    con = connect(args.db)
    domains = load_domains(args.taxonomy)

    if args.cmd == "sample":
        if args.labels.exists():
            raise SystemExit(f"{args.labels} exists - not overwritten (it may hold your labels). "
                             "Rename or delete it first, or use --labels for another file.")
        exclude, stale, by_date = exclusion(con, labeled_items(args.labels.parent, args.labels))
        args.labels.write_text(sample(con, args.n, args.seed, parse_when(args.date_from),
                                      parse_when(args.date_to, end=True), domains, exclude), encoding="utf-8")
        print(f"Wrote {len(load_labels(args.labels))} entries to {args.labels}. "
              "Open it in an editor, fill in the fields, set done: true.")
        if exclude:
            print(f"Skipped {len(exclude)} entries that belong to other enrich_labels*.yaml files: "
                  f"{stale} of the labeled entries were edited since, so {by_date} further entries "
                  "with the same date were skipped as well.")
        return

    if args.cmd == "score" and not args.labels.exists():
        raise SystemExit(f"{args.labels} not found. Run: python -m enrich.evaluate sample")
    if args.cmd == "achievements":
        ver = version(domains)
        runs_path = repo_path(ACHIEVEMENT_RUNS)
        runs = json.loads(runs_path.read_text(encoding="utf-8")) if runs_path.exists() else []
        sees_validation = args.which in ("validation", "all")
        if sees_validation and ver in runs and not args.again:
            raise SystemExit(f"The validation set was already scored with extraction version {ver}. Scoring it "
                             "again after tuning makes the figures worthless; repeat the identical run with --again.")
    try:
        require(args.model)
    except OllamaError as e:
        raise SystemExit(str(e))
    start = time.time()

    if args.cmd == "achievements":
        chat = lambda msgs, sch: chat_json(msgs, sch, model=args.model, think=args.think)  # noqa: E731
        details: list[str] | None = [] if args.details else None
        try:
            result, skipped = score_achievements(
                con, chat, domains, args.which, parse_when(args.reviewed_from), parse_when(args.reviewed_to, end=True),
                args.negatives, args.seed,
                lambda k, n: print(f"\r  {k}/{n} entries ({time.time() - start:.0f} s)", end="", file=sys.stderr,
                                   flush=True), details)
        except OllamaError as e:
            raise SystemExit(f"\n{e}")
        print(file=sys.stderr, flush=True)
        print(f"Model {args.model}{' with thinking' if args.think else ''}, {time.time() - start:.0f} s, "
              f"extraction version {ver}, set {args.which}")
        print(result)
        print("skipped: " + ", ".join(f"{k} {v}" for k, v in skipped.items()))
        if sees_validation and ver not in runs:
            runs_path.write_text(json.dumps([*runs, ver]), encoding="utf-8")
        if details is not None:
            print("\nDifferences per entry - PRIVATE content, for this screen only, do not paste it anywhere.\n",
                  file=sys.stderr, flush=True)
            print("\n".join(details) or "No differences.")
        return

    if args.cmd == "mood":
        chat = lambda msgs, sch: chat_json(msgs, sch, model=args.model)  # noqa: E731
        pairs: list = []
        try:
            notes = [] if args.no_context or not args.mood_only else load_notes(repo_path(args.notes))
        except ValueError as e:
            raise SystemExit(f"{e}\nFix the notes file or run with --no-context.")
        if any(n.context for n in notes):
            print("Rating with the background of the period notes (use --no-context to compare without).",
                  file=sys.stderr)
        try:
            result, skipped = score_mood(con, chat, domains, args.limit,
                                         lambda k, n: print(f"\r  {k}/{n} entries ({time.time() - start:.0f} s)",
                                                            end="", file=sys.stderr, flush=True), pairs,
                                         mood_only=args.mood_only, notes=notes)
        except OllamaError as e:
            raise SystemExit(f"\n{e}")
        ver = mood_version() if args.mood_only else version(domains)
        save_pairs(args.pairs, args.model, ver, pairs)
        print(file=sys.stderr, flush=True)
        print(f"Model {args.model}, {time.time() - start:.0f} s, extraction version {ver}, "
              f"against the moods you rated yourself")
        print(f"entries scored: {result.entries}")
        print("\n".join(result.mood_lines()))
        print("skipped: " + ", ".join(f"{k} {v}" for k, v in skipped.items()))
        print(f"Saved {len(pairs)} pairs of numbers (no text) to {args.pairs.name}; "
              "next: .\\evaluate.ps1 calibrate" + (" --mood-only" if args.mood_only else ""))
        return

    def progress(k: int, n: int) -> None:
        print(f"\r  {k}/{n} entries ({time.time() - start:.0f} s)", end="", file=sys.stderr, flush=True)

    chat = lambda m, sch: chat_json(m, sch, model=args.model, think=args.think)  # noqa: E731
    try:
        details: list[str] | None = [] if args.details else None
        result, skipped = score(con, load_labels(args.labels), chat, domains, progress, details)
    except OllamaError as e:
        raise SystemExit(f"\n{e}")
    print(file=sys.stderr, flush=True)
    print(f"Model {args.model}{' with thinking' if args.think else ''}, {time.time() - start:.0f} s, "
          f"extraction version {version(domains)}, labels {args.labels.name}")
    print(result)
    print("skipped: " + ", ".join(f"{k} {v}" for k, v in skipped.items()))
    if details is not None:
        print("\nDifferences per entry - PRIVATE content, for this screen only, do not paste it anywhere.\n",
              file=sys.stderr, flush=True)
        print("\n".join(details) or "No differences.")


if __name__ == "__main__":
    main()
