"""Extract structured data from one entry with the local model, then check it against the text.

Design rule: numbers come from the text, not from the model. A duration is kept only if the
entry states it (minutes, hours or h:mm); a person only if the name occurs in the entry.
Ratings (mood, energy, intensity, sentiment, severity) are the model's judgment and are labeled
as such in the schema. Long entries are split into pieces and the results merged.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from search.chunk import chunk_text

from .schema import MoodOnly, _Extraction
from .taxonomy import Domain

# (messages, json_schema) -> (content, stats)
ChatFn = Callable[[list[dict], dict], tuple[str, dict]]

MAX_CHARS = 6000  # per model call; about 2,000 tokens of German text

_INTRO = """You extract structured data from one entry of a personal diary. The entry may be German or \
English. Use only what the entry states; leave a field empty (null or []) when the entry does not say it.

Fields:
"""

# The mood rules, shared by the full extraction and the mood-only pass.
MOOD_FIELDS = """- mood_reason: first, in a few words, what sets the tone of the entry.
- mood_score: overall mood of the writer, -5 (very bad) to +5 (very good). Always give a value. Most
  entries lean one way; use 0 only if the entry has no tone at all. Mild feelings count: tired, annoyed, worried, disappointed
  -1 to -2; content, relaxed, pleased +1 to +2. Strong feelings -3 to -5 or +3 to +5 (e.g. "mies" -3,
  "verzweifelt" -5, "super Laune" +4). Weigh the whole entry, not only its last sentence.
  The tone counts even without feeling words: a good day, a success, enjoyment, relief, gratitude or
  looking forward to something is positive (+1 to +3); trouble, conflict, failure or illness is negative.
"""

_OTHER_FIELDS = """- emotions, themes: short lowercase ENGLISH words, also for German entries: "zufrieden" -> "content",
  "mies" -> "miserable"; themes e.g. "work", "family".
- energy: 1 (exhausted) to 5 (very energetic), null if not expressed. sleep: good, normal or poor.
- people: every named person, exactly as written in the entry. No pronouns, no groups, no roles or
  titles without a name ("Arzt", "Trainer", "coach"). relation: "own_life" for everyone the writer
  knows personally (family, friends, colleagues, coaches, neighbours, people met), also when the entry
  only talks about them; "public_or_watched" only for public figures and strangers the writer saw in
  the media or at an event (athletes in a match or a line-up, people on TV, artists, authors,
  politicians); "not_a_person" for names of teams, clubs, companies, places, events and brands, even
  when they look like a person's name.
- health_events: the writer's own injuries, illnesses, pain, mental health problems (not those of other
  people); condition in a few words.
  Not a health event: ordinary tiredness, sleep, diet, weight, normal muscle soreness after exercise.
  category: injury, illness (e.g. a cold, flu), pain (any pain, also mild pain) or mental (a mental
  health problem such as depression, anxiety or burnout; an ordinary bad mood or stress is not one).
  If none fits, it is not a health event.
  type: symptom, diagnosis, treatment or recovery. severity 1 (mild) to 5 (severe).
- activities: things the writer actually did in the time the entry covers, assigned to one of the
  domains below. Not plans, wishes, thoughts, or a topic merely mentioned. Everyday routines (sleeping,
  eating, working, commuting) are no activity unless the entry describes a deliberate effort in a domain. activity: a short
  lowercase ENGLISH noun, also for German entries: "Lauf" -> "running", "Dehnübungen" -> "stretching".
  duration_min only if the entry states a duration.
  intensity 1 (very easy) to 5 (maximal); sentiment -2 (disliked) to +2 (enjoyed).
- milestones: events in the writer's life, each of one kind:
  first: a first time ("zum ersten Mal", "erster Lauf") that is only a new experience, without a success;
  achievement: a success, a reached goal or a clear step towards one, also a small one (a race finished,
  an exam passed, a personal best, the longest session so far, a course module completed). When a first
  time is also a success, the kind is achievement, not first;
  setback: a difficulty or loss (an injury, giving up, a failed attempt);
  decision: a choice that changes something going forward (a new plan, stopping something).
  An entry can have several or none. A routine session is an activity, not a milestone.
  domain: one of the domains below, or "general" if none fits (e.g. work, family, home).
  Title in a few words in the language of the entry.

Domains:
{domains}"""

SYSTEM = _INTRO + MOOD_FIELDS + _OTHER_FIELDS

MOOD_SYSTEM = """You rate the mood of the writer in one entry of a personal diary. The entry may be German or \
English.

Fields:
""" + MOOD_FIELDS

_NUM = r"(\d+(?:[.,]\d+)?)"
_MINUTES = re.compile(_NUM + r"\s*(?:minuten|minutes?|mins?\b|min\b|')", re.IGNORECASE)
_HOURS = re.compile(_NUM + r"\s*(?:stunden?|std\b\.?|hours?|hrs?\b|h\b)", re.IGNORECASE)
_H_MM = re.compile(r"(?<![\d:])(\d{1,2}):([0-5]\d)(?::[0-5]\d)?(?![\d:])")


# Lines like "Stimmung = +2" are the writer's own mood rating (analysis.measurements.MOOD_KIND). The model
# must not see them: it would copy the value, and its own estimates could no longer be checked against it.
# The pattern is deliberately as loose as the near-miss counter, so misspelled markers are hidden too.
# Only the marker itself is removed: the diary text may follow it on the same line ("Stimmung = +2 Heute war ...").
MOOD_MARKER = re.compile(r"(?im)(?:^|(?<=\t))[ \t]*(?:stimmung|mood)[ \t]*[:=][ \t]*[+\-−–]?[ \t]*\d"
                         r"(?:[.,/\-–]\d+)?[ \t]*[.;]?[ \t]*\t?")


# "Achievement = 2: <sentence>" marks an achievement for tuning (analysis.measurements.ACHIEVEMENT_KIND). The
# model must not see the marker, but it does see the sentence behind it. Loose like the mood marker, so
# misspelled markers are hidden too.
ACHIEVEMENT_MARKER = re.compile(r"(?im)(?:^|(?<=\t))[ \t]*achievement[ \t]*[:=][ \t]*[+\-−–]?[ \t]*\d"
                                r"(?:[.,/\-–]\d+)?[ \t]*:?[ \t]*")


def strip_markers(text: str) -> str:
    out = []
    for line in text.split("\n"):
        cleaned = ACHIEVEMENT_MARKER.sub("", MOOD_MARKER.sub("", line))
        if cleaned != line and not cleaned.strip():
            continue  # the line held nothing but the marker
        out.append(cleaned)
    return "\n".join(out)


def system_prompt(domains: list[Domain]) -> str:
    lines = [f"- {d.name}: {d.description}" + (f" (e.g. {', '.join(d.hints)})" if d.hints else "")
             for d in domains]
    return SYSTEM.format(domains="\n".join(lines))


def stated_durations(text: str) -> set[int]:
    """All durations in minutes the text states explicitly."""
    num = lambda s: float(s.replace(",", "."))  # noqa: E731
    found = {round(num(m)) for m in _MINUTES.findall(text)}
    found |= {round(num(h) * 60) for h in _HOURS.findall(text)}
    found |= {int(h) * 60 + int(m) for h, m in _H_MM.findall(text)}
    return found


def name_in_text(name: str, text: str) -> bool:
    """Left word boundary only, so inflected forms ("Lenas") still count."""
    return len(name) >= 2 and re.search(r"(?<!\w)" + re.escape(name), text, re.IGNORECASE) is not None


@dataclass
class Checks:
    """Counts of model output dropped by the text checks (no text, counts only)."""
    durations_dropped: int = 0
    people_dropped: int = 0
    public_skipped: int = 0  # names the model marked as public, only watched, or not a person
    pieces: int = 0
    # casefolded name -> why it was not kept; in memory only, for the accuracy check (never printed)
    dropped_names: dict[str, str] = field(default_factory=dict)

    def add(self, other: Checks) -> None:
        self.durations_dropped += other.durations_dropped
        self.people_dropped += other.people_dropped
        self.public_skipped += other.public_skipped
        self.dropped_names.update(other.dropped_names)
        self.pieces += other.pieces


def _dedupe(items: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    for item in (i.strip() for i in items):
        if item and item.casefold() not in seen:
            seen[item.casefold()] = item
    return list(seen.values())


def verify(result: _Extraction, text: str, checks: Checks) -> _Extraction:
    durations = stated_durations(text)
    for a in result.activities:
        if a.duration_min is not None and not any(abs(a.duration_min - d) <= 1 for d in durations):
            a.duration_min = None
            checks.durations_dropped += 1
    own = [p.name for p in result.people if p.relation == "own_life"]
    checks.public_skipped += len(result.people) - len(own)
    for p in result.people:
        if p.relation != "own_life":
            checks.dropped_names[p.name.strip().casefold()] = p.relation
    people = _dedupe(own)
    kept = [p for p in people if name_in_text(p, text)]
    checks.people_dropped += len(people) - len(kept)
    for p in people:
        if p not in kept:
            checks.dropped_names[p.casefold()] = "not_in_text"
    result.people = kept  # from here on: names only
    result.emotions = _dedupe(result.emotions)
    result.themes = _dedupe(result.themes)
    return result


def _mean(values: list[int | None]) -> int | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals)) if vals else None


def merge(parts: list[_Extraction], model: type[_Extraction]) -> _Extraction:
    if len(parts) == 1:
        return parts[0]
    return model.model_construct(
        mood_reason="; ".join(p.mood_reason for p in parts if p.mood_reason)[:120],
        mood_score=_mean([p.mood_score for p in parts]),
        emotions=_dedupe([x for p in parts for x in p.emotions]),
        energy=_mean([p.energy for p in parts]),
        sleep=next((p.sleep for p in parts if p.sleep), None),
        themes=_dedupe([x for p in parts for x in p.themes]),
        people=_dedupe([x for p in parts for x in p.people]),
        health_events=[x for p in parts for x in p.health_events],
        activities=[x for p in parts for x in p.activities],
        milestones=[x for p in parts for x in p.milestones],
    )


def extract_entry(text: str, raw_date: str | None, chat: ChatFn, model: type[_Extraction],
                  system: str, max_chars: int = MAX_CHARS) -> tuple[_Extraction, Checks, list[dict]]:
    """Returns (checked result, check counts, Ollama stats per call).

    Raises pydantic.ValidationError or ValueError if the model's answer does not fit the schema.
    """
    schema = model.model_json_schema()
    text = strip_markers(text)
    pieces = [text] if len(text) <= max_chars else chunk_text(text, target=max_chars, overlap=0)
    checks, parts, stats = Checks(pieces=len(pieces)), [], []
    for piece in pieces:
        header = f"Date: {raw_date}\n" if raw_date else ""
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": f"{header}Entry:\n{piece}"}]
        content, call_stats = chat(messages, schema)
        stats.append(call_stats)
        parts.append(verify(model.model_validate(json.loads(content)), piece, checks))
    return merge(parts, model), checks, stats


def with_context(system: str, context: str | None) -> str:
    """The instructions plus background the writer gave for this period of the diary (see enrich.notes)."""
    if not context:
        return system
    return (system + "\n\nBackground for this period of the diary, written by the writer: " + context.strip()
            + "\nTake it into account when you judge how the writer feels overall.")


def extract_mood(text: str, raw_date: str | None, chat: ChatFn, system: str = MOOD_SYSTEM,
                 max_chars: int = MAX_CHARS, context: str | None = None) -> tuple[int, str, list[dict]]:
    """Mood of one entry (-5..+5) with the model's short reason, and the Ollama stats per call.

    Long entries are split like in the full extraction and the scores averaged. Raises pydantic's
    ValidationError or a ValueError if the answer does not fit the form.
    """
    system = with_context(system, context)
    schema = MoodOnly.model_json_schema()
    text = strip_markers(text)
    pieces = [text] if len(text) <= max_chars else chunk_text(text, target=max_chars, overlap=0)
    parts, stats = [], []
    for piece in pieces:
        header = f"Date: {raw_date}\n" if raw_date else ""
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": f"{header}Entry:\n{piece}"}]
        content, call_stats = chat(messages, schema)
        stats.append(call_stats)
        parts.append(MoodOnly.model_validate(json.loads(content)))
    reason = "; ".join(p.mood_reason for p in parts if p.mood_reason)[:120]
    return _mean([p.mood_score for p in parts]), reason, stats
