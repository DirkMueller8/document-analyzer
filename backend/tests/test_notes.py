from datetime import date

import pytest

from analysis.mood_report import (Period, build_report, from_year, main, note_marks, period_bounds, render_chart,
                                  summarize)
from enrich.evaluate import score_mood
from enrich.extract import MOOD_SYSTEM, with_context
from enrich.notes import Note, context_for, context_tag, load_chart_from, load_notes
from enrich.run import enrich_mood, mood_version, pending_mood
from ingest.store import connect, sync_entries
from test_enrich import DOMAINS, FakeModel, e

NOTES_YAML = """
chart_from: 1994
notes:
  - from: 2020-03
    to: 2020-09
    label: rehab
    note: Injury and rehabilitation; the estimates may be too mild.
    context: The writer is recovering from a serious injury. Small steps forward do not mean that the writer feels well.
  - from: 2021
    to: 2021-06-30
    note: Only a remark, no context.
"""


@pytest.fixture
def notes_file(tmp_path):
    path = tmp_path / "mood_notes.yaml"
    path.write_text(NOTES_YAML, encoding="utf-8")
    return path


def test_the_notes_file_is_read(notes_file, tmp_path):
    notes = load_notes(notes_file)
    assert [(n.start, n.end, n.label) for n in notes] == [(date(2020, 3, 1), date(2020, 9, 30), "rehab"),
                                                          (date(2021, 1, 1), date(2021, 6, 30), "")]
    assert notes[0].context.startswith("The writer is recovering") and notes[1].context == ""
    assert load_chart_from(notes_file) == 1994
    assert load_notes(tmp_path / "missing.yaml") == [] and load_chart_from(tmp_path / "missing.yaml") is None


def test_mistakes_in_the_file_are_named(tmp_path):
    path = tmp_path / "n.yaml"
    path.write_text("notes:\n  - from: 2020-03\n    note: no end\n", encoding="utf-8")
    with pytest.raises(ValueError, match="note 1: missing 'to'"):
        load_notes(path)
    path.write_text("notes:\n  - {from: 2021, to: 2020}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="'from' is after 'to'"):
        load_notes(path)
    path.write_text("chart_from: soon\n", encoding="utf-8")
    with pytest.raises(ValueError, match="chart_from must be a year"):
        load_chart_from(path)


def test_context_follows_the_dates(notes_file):
    notes = load_notes(notes_file)
    assert context_for(notes, "2020-05-02").startswith("The writer is recovering")
    assert context_for(notes, "2020-10-01") == "" and context_for(notes, "2021-02-01") == ""   # no context there
    assert context_for(notes, None) == "" and context_for([], "2020-05-02") == ""
    both = [Note(date(2020, 1, 1), date(2020, 12, 31), context="A."), Note(date(2020, 6, 1), date(2020, 6, 30), context="B.")]
    assert context_for(both, "2020-06-15") == "A. B." and context_for(both, "2020-02-01") == "A."
    assert context_tag("") == "" and context_tag("A.") != context_tag("B.") and context_tag("A.") == context_tag("A.")
    assert with_context(MOOD_SYSTEM, None) == MOOD_SYSTEM
    assert "Background for this period of the diary, written by the writer: Text." in with_context(MOOD_SYSTEM, " Text. ")


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "t.db")
    sync_entries(c, [e(date(2019, 5, 1), "Radtour 1", 1), e(date(2020, 5, 2), "Radtour 2", 2),
                     e(date(2020, 6, 3), "Radtour 3", 3), e(date(2022, 1, 4), "Radtour 4", 4)])
    return c


def test_only_the_entries_of_a_period_with_a_context_are_rated_again(con, notes_file):
    seen = []

    def chat(messages, schema):
        seen.append(messages[0]["content"])
        return FakeModel()(messages, schema)

    assert enrich_mood(con, chat, "fake").done == 4                            # first run without notes
    assert not any("Background" in s for s in seen)
    notes = load_notes(notes_file)
    assert len(pending_mood(con, mood_version(), notes=notes)) == 2            # the two entries of 2020
    seen.clear()
    assert enrich_mood(con, chat, "fake", notes=notes).done == 2
    assert len(seen) == 2 and all("recovering from a serious injury" in s for s in seen)
    versions = [r[0] for r in con.execute("SELECT version FROM entry_meta ORDER BY entry_id")]
    assert versions[0] == versions[3] == mood_version() and versions[1] == versions[2] == mood_version() + context_tag(
        notes[0].context)
    assert enrich_mood(con, chat, "fake", notes=notes).done == 0               # nothing left to do
    changed = [Note(notes[0].start, notes[0].end, context="Another background.")]
    assert enrich_mood(con, chat, "fake", notes=changed).done == 2             # a changed context redoes them again
    assert enrich_mood(con, chat, "fake").done == 2                            # and so does removing it


def test_the_accuracy_check_uses_the_same_background(con, notes_file):
    seen = []

    def chat(messages, schema):
        seen.append(messages[0]["content"])
        return FakeModel()(messages, schema)

    with con:
        con.execute("UPDATE entries SET text = text || char(10) || 'Stimmung = -2' WHERE date_start = '2020-05-02'")
        con.execute("UPDATE entries SET text = text || char(10) || 'Stimmung = 1' WHERE date_start = '2022-01-04'")
    from analysis.measurements import MOOD_KIND, store_all
    store_all(con, [MOOD_KIND])
    result, _ = score_mood(con, chat, DOMAINS, mood_only=True, notes=load_notes(notes_file))
    assert result.entries == 2 and sum("Background" in s for s in seen) == 1   # only the 2020 entry has a context
    seen.clear()
    score_mood(con, chat, DOMAINS, mood_only=True)
    assert not any("Background" in s for s in seen)


def test_period_bounds():
    assert period_bounds("2020", "year") == (date(2020, 1, 1), date(2020, 12, 31))
    assert period_bounds("2020 H1", "half") == (date(2020, 1, 1), date(2020, 6, 30))
    assert period_bounds("2020 H2", "half") == (date(2020, 7, 1), date(2020, 12, 31))
    assert period_bounds("2020 Q1", "quarter") == (date(2020, 1, 1), date(2020, 3, 31))
    assert period_bounds("2020 Q4", "quarter") == (date(2020, 10, 1), date(2020, 12, 31))
    assert period_bounds("2020-02", "month") == (date(2020, 2, 1), date(2020, 2, 29))


def synthetic_periods(years):
    return summarize([(str(y), float(v), "estimated") for y in years for v in (1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0)])


def test_marks_and_note_lines(notes_file):
    notes = load_notes(notes_file)
    marks, lines = note_marks(synthetic_periods(range(2019, 2023)), "year", notes)
    assert marks == {"2020": "#", "2021": "#"}
    assert lines == ["# 2020-03 to 2020-09: Injury and rehabilitation; the estimates may be too mild. "
                     "The model was told this background.", "# 2021-01 to 2021-06: Only a remark, no context."]
    assert note_marks(synthetic_periods([2019]), "year", notes) == ({}, [])


def test_the_table_shows_the_marks(con, notes_file, tmp_path):
    from enrich.run import store_mood
    for entry_id, h in con.execute("SELECT id, text_hash FROM entries").fetchall():
        store_mood(con, entry_id, h, 1, "fake", "mood1:x")
    text, _ = build_report(con, "year", 1, notes=load_notes(notes_file))
    rows = {line[:6].strip(): line for line in text.splitlines()}
    assert "2020#" in rows and "2019#" not in rows and "2022#" not in rows
    assert "# 2020-03 to 2020-09: Injury and rehabilitation" in text


def test_the_chart_can_start_in_a_given_year(con, notes_file, tmp_path, capsys):
    periods = synthetic_periods(range(1990, 1998))
    assert [p.key for p in from_year(periods, 1994)] == ["1994", "1995", "1996", "1997"]
    assert from_year(periods, None) == periods and from_year(periods, 2100) == []
    png = tmp_path / "with_notes.png"
    render_chart(synthetic_periods(range(2019, 2023)), "year", 10, png, load_notes(notes_file))
    assert png.stat().st_size > 5000                                            # bands and label drawn without error
    # chart_from comes from the notes file, --chart-from overrides it, --no-notes ignores the file
    from enrich.run import store_mood
    for entry_id, h in con.execute("SELECT id, text_hash FROM entries").fetchall():
        store_mood(con, entry_id, h, 1, "fake", "mood1:x")
    con.close()
    db = tmp_path / "t.db"
    main(["--db", str(db), "--notes", str(notes_file), "--chart", str(tmp_path / "a.png")])
    assert "No chart: no period starts in 1994" not in capsys.readouterr().out and (tmp_path / "a.png").exists()
    main(["--db", str(db), "--notes", str(notes_file), "--chart-from", "2030", "--chart", str(tmp_path / "b.png")])
    assert "No chart: no period starts in 2030 or later." in capsys.readouterr().out
    assert not (tmp_path / "b.png").exists()
    main(["--db", str(db), "--notes", str(notes_file), "--no-notes", "--chart", str(tmp_path / "c.png")])
    assert (tmp_path / "c.png").exists() and "# 2020-03" not in capsys.readouterr().out
