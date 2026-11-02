import json
import sqlite3
from datetime import date

import pytest
from pydantic import ValidationError

from enrich.extract import Checks, extract_entry, name_in_text, stated_durations, system_prompt, verify
from enrich.run import RunStats, enrich, ensure_columns, pending, show, spread, summary, version
from enrich.schema import build_model
from enrich.taxonomy import Domain, fingerprint, load_domains
from ingest.segment import Entry
from ingest.store import connect, sync_entries
from search import ollama_client

DOMAINS = [Domain("endurance", "Running, cycling", ("laufen", "rad")),
           Domain("strength", "Gym", ("krafttraining",))]


def e(d, text, pos):
    return Entry(d, d, "day", d.strftime("%d.%m.%Y"), pos, "plain", [text])


ENTRIES = [
    e(date(2005, 4, 3), "Radtour mit Jonas, 2 Stunden, mittel. Sehr gute Laune.", 1),
    e(date(2005, 4, 10), "Erster Halbmarathon mit dem Laufclub! 1:52 h, intensiv. Knie zwickt.", 2),
    e(date(2021, 3, 5), "Krafttraining 45 Minuten. Danach mit Jonas Kaffee.", 3),
]

def own(*names):
    return [{"name": n, "relation": "own_life"} for n in names]


ANSWERS = {  # what a well-behaved model would answer, keyed by a word of the entry
    "Radtour": {"mood_score": 4, "emotions": ["joy"], "people": own("Jonas", "Mia"),  # Mia: not in the text
                "activities": [{"domain": "endurance", "activity": "cycling", "duration_min": 120, "intensity": 3}]},
    "Halbmarathon": {"mood_score": 5, "health_events": [{"condition": "knee pain", "category": "pain"}],
                     "activities": [{"domain": "endurance", "activity": "running", "duration_min": 112}],
                     "milestones": [{"domain": "endurance", "title": "Erster Halbmarathon", "kind": "first"}]},
    "Krafttraining": {"people": own("Jonas") + [{"name": "Kaffee", "relation": "public_or_watched"}],
                      "activities": [{"domain": "strength", "activity": "weights", "duration_min": 60}]},  # 45 stated
}


class FakeModel:
    def __init__(self, broken: set[str] = frozenset()):
        self.calls = 0
        self.broken = broken

    def __call__(self, messages, schema):
        self.calls += 1
        text = messages[-1]["content"]
        key = next(k for k in ANSWERS if k in text) if any(k in text for k in ANSWERS) else None
        if key in self.broken:
            return "{not json", {}
        answer = {"mood_reason": "", "mood_score": 0, **ANSWERS.get(key, {})}
        return json.dumps(answer), {"eval_count": 10, "eval_duration": 1e9}


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "t.db")
    sync_entries(c, ENTRIES)
    return c


def test_schema_restricts_domains_and_ranges():
    model = build_model(["endurance", "strength"])
    schema = json.dumps(model.model_json_schema())
    assert '"enum": ["endurance", "strength"]' in schema
    assert "mood_score" in model.model_json_schema()["required"]
    ok = {"mood_reason": "", "mood_score": 0}
    model.model_validate({**ok, "activities": [{"domain": "strength", "activity": "squats"}]})
    with pytest.raises(ValidationError):
        model.model_validate({**ok, "activities": [{"domain": "chess", "activity": "blitz"}]})
    with pytest.raises(ValidationError):
        model.model_validate({**ok, "mood_score": 9})
    model.model_validate({**ok, "milestones": [{"domain": "general", "title": "Neuer Job", "kind": "decision"}]})
    with pytest.raises(ValidationError):  # "general" is for milestones only
        model.model_validate({**ok, "activities": [{"domain": "general", "activity": "work"}]})
    with pytest.raises(ValidationError):  # no catch-all health category any more
        model.model_validate({**ok, "health_events": [{"condition": "tired", "category": "other"}]})
    assert list(model.model_json_schema()["properties"])[:2] == ["mood_reason", "mood_score"]  # reason first
    with pytest.raises(ValidationError):
        model.model_validate({})                                    # mood is required
    with pytest.raises(ValueError):
        build_model([])


def test_stated_durations():
    assert stated_durations("2 Stunden locker, dann 15 min Dehnen, 1:52 h, 1,5 h, 45'") >= {120, 15, 112, 90, 45}
    assert stated_durations("Schnitt 12,5 km/h und 3x am Tag") == set()
    assert 120 not in stated_durations("am 12.03. um 2 Uhr")


def test_verify_drops_unstated_numbers_and_unknown_names():
    model = build_model(["endurance"])
    result = model.model_validate({"mood_reason": "", "mood_score": 1, "people": own("Lena", "lena", "Tom") + [
                                       {"name": "Petrov", "relation": "public_or_watched"},
                                       {"name": "Laufgruppe", "relation": "not_a_person"}],
                                   "emotions": ["joy", "Joy"],
                                   "activities": [{"domain": "endurance", "activity": "running", "duration_min": 50},
                                                  {"domain": "endurance", "activity": "cycling", "duration_min": 30}]})
    checks = Checks()
    verify(result, "Mit Lenas Laufgruppe 30 Minuten gelaufen, danach Petrov im Fernsehen.", checks)
    assert result.people == ["Lena"] and result.emotions == ["joy"]
    assert [a.duration_min for a in result.activities] == [None, 30]
    assert (checks.durations_dropped, checks.people_dropped, checks.public_skipped) == (1, 1, 2)
    assert checks.dropped_names == {"petrov": "public_or_watched", "laufgruppe": "not_a_person",
                                    "tom": "not_in_text"}
    with pytest.raises(ValidationError):
        model.model_validate({"mood_reason": "", "mood_score": 0, "people": ["Lena"]})  # a relation is required
    assert not name_in_text("A", "A day")


def test_enrich_stores_checked_results(con):
    stats = enrich(con, FakeModel(), DOMAINS, "fake")
    assert (stats.done, stats.failed) == (3, 0)
    assert stats.checks.people_dropped == 1 and stats.checks.durations_dropped == 1
    assert stats.checks.public_skipped == 1
    q = lambda sql: con.execute(sql).fetchall()  # noqa: E731
    assert q("SELECT canonical_name FROM people") == [("Jonas",)]
    assert q("SELECT COUNT(*) FROM mentions") == [(2,)]
    assert q("SELECT activity, duration_min FROM activities ORDER BY entry_id") == [
        ("cycling", 120), ("running", 112), ("weights", None)]
    assert q("SELECT kind FROM milestones") == [("first",)]
    assert q("SELECT model, version FROM entry_meta LIMIT 1") == [("fake", version(DOMAINS))]
    assert "activities per domain: endurance 2, strength 1" in summary(con)
    assert "tokens/s" in str(stats)


def test_enrich_is_resumable_and_follows_changes(con):
    model = FakeModel()
    enrich(con, model, DOMAINS, "fake", limit=2)
    assert len(pending(con, version(DOMAINS))) == 1
    enrich(con, model, DOMAINS, "fake")
    assert model.calls == 3
    assert enrich(con, model, DOMAINS, "fake").done == 0             # nothing left
    sync_entries(con, ENTRIES[:2] + [e(date(2021, 3, 5), "Krafttraining 60 Minuten.", 3)])
    assert enrich(con, model, DOMAINS, "fake").done == 1             # only the edited entry
    assert con.execute("SELECT duration_min FROM activities WHERE activity = 'weights'").fetchone() == (60,)
    assert con.execute("SELECT COUNT(*) FROM mentions").fetchone() == (1,)  # old mention went with the entry
    changed = DOMAINS + [Domain("mobility", "Yoga", ())]
    assert fingerprint(changed) != fingerprint(DOMAINS)
    assert enrich(con, model, changed, "fake").done == 3             # new taxonomy: everything again
    assert con.execute("SELECT COUNT(*) FROM activities").fetchone() == (3,)  # replaced, not duplicated
    sync_entries(con, ENTRIES[1:2] + [e(date(2021, 3, 5), "Krafttraining 60 Minuten.", 3)])
    enrich(con, model, changed, "fake")
    assert con.execute("SELECT COUNT(*) FROM people").fetchone() == (0,)   # nobody mentions Jonas any more


def test_prompt_change_triggers_reextraction(con, monkeypatch):
    import enrich.extract as extract_module

    enrich(con, FakeModel(), DOMAINS, "fake")
    monkeypatch.setattr(extract_module, "SYSTEM", extract_module.SYSTEM + "\nOne more rule.")
    assert enrich(con, FakeModel(), DOMAINS, "fake").done == 3


def test_date_range_and_even_spread(tmp_path):
    c = connect(tmp_path / "t.db")
    sync_entries(c, [e(date(y, m, 1), f"Radtour {y}-{m}", y * 100 + m) for y in range(2006, 2013) for m in (1, 7)])
    assert spread(list(range(10)), 3) == [0, 3, 6]
    assert spread([1, 2], 5) == [1, 2]
    stats = enrich(c, FakeModel(), DOMAINS, "fake", limit=3, even=True,
                   date_from=date(2008, 1, 1), date_to=date(2010, 12, 31))
    assert (stats.pending, stats.done) == (6, 3)
    got = [r[0] for r in c.execute("SELECT e.date_start FROM entry_meta m JOIN entries e ON e.id = m.entry_id"
                                   " ORDER BY 1")]
    assert got == ["2008-01-01", "2009-01-01", "2010-01-01"]          # spread, all inside the range
    assert enrich(c, FakeModel(), DOMAINS, "fake", date_from=date(2008, 1, 1), date_to=date(2010, 12, 31)).done == 3
    assert len(pending(c, version(DOMAINS))) == 8                      # 2006-2007 and 2011-2012 untouched


def test_failures_are_counted_and_retried(con):
    stats = enrich(con, FakeModel(broken={"Halbmarathon"}), DOMAINS, "fake")
    assert (stats.done, stats.failed) == (2, 1) and len(stats.failed_ids) == 1
    assert enrich(con, FakeModel(), DOMAINS, "fake").done == 1


def test_stops_after_failures_in_a_row(tmp_path):
    c = connect(tmp_path / "t.db")
    sync_entries(c, [e(date(2005, 1, d), f"Halbmarathon Nummer {d}", d) for d in range(1, 8)])
    with pytest.raises(RuntimeError, match="in a row"):
        enrich(c, FakeModel(broken={"Halbmarathon"}), DOMAINS, "fake")


def test_people_match_aliases(con):
    con.execute("INSERT INTO people (canonical_name, aliases) VALUES ('Jonas Berg', '[\"jonas\"]')")
    enrich(con, FakeModel(), DOMAINS, "fake")
    assert con.execute("SELECT COUNT(*) FROM people").fetchone() == (1,)
    assert con.execute("SELECT COUNT(*) FROM mentions").fetchone() == (2,)


def test_long_entries_are_split_and_merged():
    model = build_model(["endurance"])
    text = "\n".join(["Radtour, 2 Stunden. " + "Wind " * 25, "Halbmarathon, 1:52 h. " + "Regen " * 25])
    fake = FakeModel()
    result, checks, calls = extract_entry(text, "10.04.2005", fake, model, system_prompt(DOMAINS[:1]),
                                          max_chars=200)
    assert checks.pieces == 2 and len(calls) == 2
    assert result.mood_score == 4  # mean of 4 and 5, rounded half to even
    assert [a.activity for a in result.activities] == ["cycling", "running"]
    assert [a.duration_min for a in result.activities] == [120, 112]  # each checked against its own piece


def test_old_databases_get_the_new_columns(tmp_path):
    raw = sqlite3.connect(tmp_path / "old.db")
    raw.execute("CREATE TABLE entry_meta (entry_id INTEGER PRIMARY KEY, text_hash TEXT NOT NULL,"
                " mood_score INTEGER, emotions TEXT, energy INTEGER, sleep TEXT, themes TEXT)")
    ensure_columns(raw)
    ensure_columns(raw)  # idempotent
    assert {"model", "version", "extracted_at"} <= {r[1] for r in raw.execute("PRAGMA table_info(entry_meta)")}


def test_example_taxonomy_loads():
    domains = load_domains()
    assert domains and all(d.name and d.description for d in domains)
    assert "Domains:" in system_prompt(domains)


def test_chat_json_sends_schema_to_loopback_only(monkeypatch):
    sent = {}

    def fake_post(path, payload, timeout=600, stream=False):
        sent.update(payload)
        return {"message": {"content": "{}"}, "eval_count": 3}

    monkeypatch.setattr(ollama_client, "_post", fake_post)
    content, stats = ollama_client.chat_json([], {"type": "object"}, model="qwen3:14b")
    assert content == "{}" and stats["eval_count"] == 3
    assert sent["format"] == {"type": "object"} and sent["options"]["temperature"] == 0
    with pytest.raises(ollama_client.OllamaError, match="cloud"):
        ollama_client.chat_json([], {}, model="some-model-cloud")
    monkeypatch.setenv("OLLAMA_HOST", "http://example.com:11434")
    with pytest.raises(ollama_client.OllamaError, match="loopback"):
        ollama_client.base_url()


def test_show_lists_results_per_entry(con):
    assert show(con) == "Nothing extracted yet."
    enrich(con, FakeModel(), DOMAINS, "fake")
    text = show(con, 2)
    assert "[03.04.2005]" not in text                                    # only the last two entries
    assert text.index("[10.04.2005]") < text.index("[05.03.2021]")       # oldest first
    assert "milestone: first · endurance · Erster Halbmarathon" in text
    assert "health: knee pain (pain, symptom, severity -)" in text
    assert "people: Jonas" in text and "activity: strength / weights, intensity -, enjoyed -" in text
    assert "mood +4" in show(con, 3)


def test_run_stats_text_has_counts_only():
    assert str(RunStats(done=2, failed=1, pending=5)).startswith("2 extracted, 1 failed, 2 left")
