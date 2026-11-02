from datetime import date

import pytest

from analysis.measurements import MOOD_KIND, store_all
from enrich import run
from enrich.evaluate import score_mood
from enrich.extract import MOOD_FIELDS, MOOD_SYSTEM, SYSTEM, extract_mood
from enrich.run import describe_mood, enrich, enrich_mood, mood_version, pending, pending_mood
from enrich.schema import MoodOnly
from ingest.segment import Entry
from ingest.store import connect, sync_entries
from test_enrich import DOMAINS, FakeModel, e


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "t.db")
    sync_entries(c, [
        e(date(2005, 4, 3), "Radtour mit Jonas, 2 Stunden.\nStimmung = +1", 1),    # rated 1; the fake model says 4
        e(date(2005, 4, 10), "Erster Halbmarathon, 1:52 h.", 2),
        e(date(2021, 3, 5), "Krafttraining 45 Minuten.", 3),
        Entry(None, None, None, None, 4, "plain", ["Radtour ohne Datum."]),
    ])
    store_all(c, [MOOD_KIND])
    return c


def test_the_mood_rules_are_shared_with_the_full_prompt():
    assert MOOD_FIELDS in SYSTEM and MOOD_FIELDS in MOOD_SYSTEM
    assert "people" not in MOOD_SYSTEM and "milestones" not in MOOD_SYSTEM and "{domains}" not in MOOD_SYSTEM
    assert list(MoodOnly.model_json_schema()["properties"]) == ["mood_reason", "mood_score"]   # reason first


def test_extract_mood_hides_the_marker_and_averages_pieces():
    seen = []

    def chat(messages, schema):
        seen.append(messages[-1]["content"])
        return FakeModel()(messages, schema)

    score, reason, stats = extract_mood("Radtour mit Jonas.\nStimmung = -4\nSpaeter Halbmarathon.", "05.03.2021", chat)
    assert score == 4 and seen and all("Stimmung" not in s and "-4" not in s for s in seen)   # one piece: the model's 4
    text = "\n".join(["Radtour, 2 Stunden. " + "Wind " * 25, "Halbmarathon, 1:52 h. " + "Regen " * 25])
    score, reason, stats = extract_mood(text, None, FakeModel(), max_chars=200)
    assert len(stats) == 2 and score == 4        # mean of 4 and 5, rounded half to even


def test_only_the_mood_is_stored_and_the_run_is_resumable(con):
    stats = enrich_mood(con, FakeModel(), "fake", limit=1)
    assert (stats.done, stats.pending) == (1, 3)                       # the undated entry is not pending
    assert enrich_mood(con, FakeModel(), "fake").done == 2
    assert enrich_mood(con, FakeModel(), "fake").done == 0
    rows = con.execute("SELECT mood_score, version, model, emotions, energy FROM entry_meta ORDER BY entry_id").fetchall()
    assert rows == [(4, mood_version(), "fake", None, None), (5, mood_version(), "fake", None, None),
                    (0, mood_version(), "fake", None, None)]
    for table in run.CHILD_TABLES:
        assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
    assert describe_mood(stats).startswith("1 rated, 0 failed, 2 left")


def test_the_view_still_prefers_the_rated_value(con):
    enrich_mood(con, FakeModel(), "fake")
    rows = con.execute("SELECT mood, source FROM entry_mood WHERE date_start IS NOT NULL ORDER BY entry_id").fetchall()
    assert rows == [(1.0, "rated"), (5, "estimated"), (0, "estimated")]


def test_date_range_and_even_spread(con):
    stats = enrich_mood(con, FakeModel(), "fake", date_from=date(2021, 1, 1))
    assert (stats.pending, stats.done) == (1, 1)
    assert enrich_mood(con, FakeModel(), "fake", limit=1, even=True).done == 1


def test_full_extractions_stay_and_a_full_run_replaces_the_mood_only_rows(con):
    enrich(con, FakeModel(), DOMAINS, "fake", date_to=date(2005, 4, 5))          # full result for the first entry
    full_version = con.execute("SELECT version FROM entry_meta").fetchone()[0]
    assert enrich_mood(con, FakeModel(), "fake").done == 2                      # the others only get the mood
    assert con.execute("SELECT version FROM entry_meta ORDER BY entry_id").fetchall() == [
        (full_version,), (mood_version(),), (mood_version(),)]
    assert len(pending(con, full_version)) == 3             # two mood-only rows, plus the undated entry (full run only)
    assert enrich(con, FakeModel(), DOMAINS, "fake").done == 3
    assert {r[0] for r in con.execute("SELECT version FROM entry_meta")} == {full_version}
    assert con.execute("SELECT COUNT(*) FROM entry_meta").fetchone() == (4,)


def test_an_old_result_without_a_mood_is_filled_in(con):
    ids = [r[0] for r in con.execute("SELECT id FROM entries WHERE date_start IS NOT NULL ORDER BY id")]
    h = lambda i: con.execute("SELECT text_hash FROM entries WHERE id = ?", (i,)).fetchone()[0]  # noqa: E731
    with con:   # an early test extraction without a mood, and a full extraction that has one
        con.execute("INSERT INTO entry_meta (entry_id, text_hash, mood_score, version) VALUES (?, ?, NULL, '3:old')",
                    (ids[0], h(ids[0])))
        con.execute("INSERT INTO entry_meta (entry_id, text_hash, mood_score, version) VALUES (?, ?, 2, '6:full')",
                    (ids[1], h(ids[1])))
    assert [r[0] for r in pending_mood(con, mood_version())] == [ids[0], ids[2]]       # not the one with a mood
    enrich_mood(con, FakeModel(), "fake")
    assert con.execute("SELECT mood_score, version FROM entry_meta WHERE entry_id = ?", (ids[1],)).fetchone() == (2, "6:full")
    assert con.execute("SELECT COUNT(*) FROM entry_meta WHERE mood_score IS NULL").fetchone() == (0,)


def test_a_changed_mood_prompt_redoes_only_mood_only_rows(con, monkeypatch):
    enrich_mood(con, FakeModel(), "fake")
    monkeypatch.setattr(run, "MOOD_SYSTEM", MOOD_SYSTEM + "\nOne more rule.")
    assert len(pending_mood(con, mood_version())) == 3
    assert enrich_mood(con, FakeModel(), "fake").done == 3


def test_failures_are_counted_and_retried(con):
    stats = enrich_mood(con, lambda m, s: ("{not json", {}), "fake")
    assert stats.failed == 3 and stats.done == 0 and len(stats.failed_ids) == 3
    assert enrich_mood(con, FakeModel(), "fake").done == 3


def test_the_accuracy_check_can_use_the_mood_only_pass(con):
    pairs: list = []
    result, skipped = score_mood(con, FakeModel(), DOMAINS, pairs=pairs, mood_only=True)
    assert [p[1:3] for p in pairs] == [(1, 4)] and result.mood_both == 1 and skipped["failed"] == 0
