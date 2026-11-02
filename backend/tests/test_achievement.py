import json
from datetime import date

import pytest

from analysis.measurements import (ACHIEVEMENT_KIND, achievement_variant_shapes, count_ignored_variants, extract,
                                   load_kinds, store_all)
from enrich import evaluate
from enrich.evaluate import AchievementScore, achievement_set, score_achievements
from enrich.extract import extract_entry, strip_markers, system_prompt
from enrich.run import version
from enrich.schema import build_model
from ingest.store import connect, sync_entries
from test_enrich import DOMAINS, e

DAY = date(2005, 4, 3)


def marks(text: str) -> list[float]:
    return [m.value for m in extract(text, DAY, [ACHIEVEMENT_KIND])]


def test_the_format_is_read():
    assert marks("Achievement = 1") == [1.0] and marks("Achievement = 2") == [2.0] and marks("Achievement = 3") == [3.0]
    assert marks("achievement=2") == [2.0] and marks("  Achievement  =  3  ") == [3.0]
    assert marks("Lauf mit Jonas\nAchievement = 1\nKaffee") == [1.0]
    assert marks("Lauf\tAchievement = 2\tRest") == [2.0]                              # table cell
    assert marks("Achievement = 2\nAchievement = 3") == [2.0, 3.0]                  # several in one entry
    # what follows is free: a colon and the sentence, or the sentence on the next line (Shift+Return)
    assert marks("Achievement = 2: Lauf-Trainerschein bestanden.") == [2.0]
    assert marks("Achievement = 2 Bestzeit") == [2.0]
    assert marks("Achievement = 3:\nLauf-Trainerschein bestanden.") == [3.0]


def test_everything_else_is_a_counted_near_miss_and_never_stored():
    shapes = {
        "Achievement = +2: Bestzeit": "plus sign", "Achievement = -1: Bestzeit": "minus sign",
        "Achievement: 2": "colon instead of '='", "Achievement 2": "no '=' sign",
        "Achievement = 4": "value outside 1..3", "Achievement = 0": "value outside 1..3",
        "Achievement = 10": "value outside 1..3", "Achievement = 2,5": "decimal value",
        "Achievement = 2-3": "other", "Achievement = 2x": "other",
        "Heute Achievement = 2": "text before the marker", "Ein Achievement für mich.": "text before the marker",
        "Achievement =": "no value on the same line", "Achievement = zwei": "no value on the same line",
        "Achievement": "no value on the same line", "Achievement = Platz 2": "other characters between the word and the value",
    }
    for line, shape in shapes.items():
        assert marks(line) == [], line
        assert count_ignored_variants(line, [ACHIEVEMENT_KIND]) == {"achievement": 1}, line
        assert achievement_variant_shapes(line) == {shape: 1}, line
    # a typo in the word itself resembles nothing else, so it is recognised by its spelling
    for typo in ("Achievment = 2", "Acheievement = 2: Bestzeit", "Achivement=3", "Achievements = 1", "Lauf\tAchievemnt = 2"):
        assert marks(typo) == [] and achievement_variant_shapes(typo) == {"keyword misspelled": 1}, typo
    for prose in ("Das war ein großer Erfolg.", "Erfolg = 2", "Ruhetag", "Training = 2", "Abschluss = 3"):
        assert marks(prose) == [] and achievement_variant_shapes(prose) == {}, prose


def test_ref_prefix_marks_the_word_as_ordinary_text():
    # "Ref_" in front of the word says: this is prose, not a marker. It is neither read, nor counted as a
    # near miss or a typo, and the model keeps seeing the line unchanged.
    for prose in ("Ref_Achievement = 2", "Das war ein Ref_Achievement.", "Ref_Achievement", "Lauf\tRef_Achievement = 2",
                  "Heute Ref_Achievement = 3: Bestzeit"):
        assert marks(prose) == [], prose
        assert count_ignored_variants(prose, [ACHIEVEMENT_KIND]) == {}, prose
        assert achievement_variant_shapes(prose) == {}, prose
        assert strip_markers(prose) == prose, prose
    # the same sentence without the prefix is still a counted near miss
    assert achievement_variant_shapes("Das war ein Achievement.") == {"text before the marker": 1}


def test_sync_report_names_the_entry_dates_of_unread_markers():
    from ingest.run import unread_marker_lines
    from ingest.segment import Entry
    entries = [Entry(date(2005, 4, 3), date(2005, 4, 3), "day", "03.04.2005", 1, "plain", ["Achievment = 2"]),
               Entry(date(2005, 4, 9), date(2005, 4, 9), "day", "09.04.2005", 2, "plain", ["Achievement = 2"]),
               Entry(date(2005, 4, 10), date(2005, 4, 10), "day", "10.04.2005", 3, "plain", ["Achievemnt = 1"])]
    assert unread_marker_lines(entries, "Achievement", achievement_variant_shapes) == [
        "Achievement lines that are not read, by shape: keyword misspelled 2",
        "  keyword misspelled: 03.04.2005, 10.04.2005"]
    assert unread_marker_lines(entries[1:2], "Achievement", achievement_variant_shapes) == []


def test_the_model_never_sees_the_marker_but_sees_the_sentence():
    assert strip_markers("Achievement = 2: Lauf-Trainerschein bestanden.\nRadtour.") == \
        "Lauf-Trainerschein bestanden.\nRadtour."
    assert strip_markers("Achievement = 3:\nBestzeit") == "Bestzeit" and strip_markers("Achievement = 1\nLauf") == "Lauf"
    assert strip_markers("Lauf\tAchievement = 2 Bestzeit") == "Lauf\tBestzeit"
    assert strip_markers("Achievement = +2: Bestzeit") == "Bestzeit"               # a misspelled marker is hidden too
    assert strip_markers("Erfolg = 2 Siege") == "Erfolg = 2 Siege"                   # ordinary prose stays
    assert strip_markers("Stimmung = +1\nAchievement = 2: Bestzeit") == "Bestzeit"   # both kinds of marker
    seen = []

    def chat(messages, schema):
        seen.append(messages[-1]["content"])
        return json.dumps({"mood_reason": "", "mood_score": 0}), {}

    extract_entry("Achievement = 2: Lauf-Trainerschein bestanden.", "03.04.2005", chat, build_model(["endurance"]),
                  system_prompt(DOMAINS[:1]))
    assert seen and all("Achievement" not in s and "Trainerschein bestanden" in s for s in seen)


def fake_chat(messages, schema):
    """A model that reports an achievement for 'bestanden' and a first time for 'Halbmarathon'."""
    text = messages[-1]["content"]
    kind = "achievement" if "bestanden" in text else "first" if "Halbmarathon" in text else None
    milestones = [{"domain": "endurance", "title": "Titel", "kind": kind}] if kind else []
    return json.dumps({"mood_reason": "", "mood_score": 0, "milestones": milestones}), {}


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "t.db")
    sync_entries(c, [
        e(date(2005, 4, 3), "Achievement = 2: Lauf-Trainerschein bestanden.\nRadtour 40 km.", 1),
        e(date(2005, 4, 10), "Achievement = 3:\nErster Halbmarathon unter zwei Stunden geschafft.", 2),
        e(date(2005, 4, 17), "Achievement = 1: Neue Bestzeit über 5 km.", 3),
        e(date(2005, 4, 20), "Ruhetag, Spaziergang.", 4),
        e(date(2005, 4, 24), "Prüfung bestanden, ich war zufrieden.", 5),
    ])
    store_all(c, load_kinds(tmp_path / "missing.yaml"))
    return c


def test_markers_are_stored_as_measurements(con):
    rows = con.execute("SELECT date, value FROM measurements WHERE kind = 'achievement' ORDER BY date").fetchall()
    assert rows == [("2005-04-03", 2.0), ("2005-04-10", 3.0), ("2005-04-17", 1.0)]


def test_score_compares_with_the_marked_entries(con):
    details: list[str] = []
    result, skipped = score_achievements(con, fake_chat, DOMAINS, "all", negatives=10, details=details)
    assert result.by_level == {1: [1, 0, 0], 2: [1, 1, 0], 3: [1, 0, 1]}          # level -> entries, found, only other kind
    assert (result.marked, result.model_total) == (3, 1)
    assert (result.unmarked, result.flagged, result.flagged_total) == (2, 1, 1)
    assert skipped == {"failed": 0}
    assert len(details) == 3 and "[10.04.2005]" in details[0] and "marked 3" in details[0] and "first: Titel" in details[0]
    assert "[17.04.2005]" in details[1] and "marked 1" in details[1] and details[1].endswith("none")
    assert "[24.04.2005]" in details[2] and "not marked" in details[2]
    text = str(result)
    assert "marked entries scored: 3; the model found an achievement in 1 (33 %)" in text
    assert "marked 3: 1 entries, found 0 (0 %), only another kind 1, nothing 0" in text
    assert "the model reports an achievement in 1 (50 %), 1 in all" in text


def test_unmarked_entries_count_only_inside_the_reviewed_period(con):
    result, _ = score_achievements(con, fake_chat, DOMAINS, "all", reviewed_from=date(2005, 4, 21), negatives=10)
    assert (result.unmarked, result.flagged) == (1, 1)
    assert score_achievements(con, fake_chat, DOMAINS, "all")[0].unmarked == 0     # no --negatives: none scored
    assert "--reviewed-from" in str(AchievementScore())                          # nothing to divide by: a hint, no crash


def test_tuning_and_validation_sets_split_the_marked_entries(con):
    assert {achievement_set(f"2005-04-{d:02d}") for d in range(1, 29)} == {"tune", "validation"}
    assert achievement_set("2005-04-03") == achievement_set("2005-04-03")           # stable
    tune = score_achievements(con, fake_chat, DOMAINS, "tune")[0]
    validation = score_achievements(con, fake_chat, DOMAINS, "validation")[0]
    assert tune.marked + validation.marked == 3


def test_the_validation_set_is_scored_once_per_version(con, tmp_path, monkeypatch):
    runs = tmp_path / "runs.json"
    runs.write_text(json.dumps([version(DOMAINS)]), encoding="utf-8")
    monkeypatch.setattr(evaluate, "ACHIEVEMENT_RUNS", runs)
    monkeypatch.setattr(evaluate, "load_domains", lambda path=None: DOMAINS)
    for which in ("validation", "all"):
        with pytest.raises(SystemExit, match="already scored"):
            evaluate.main(["achievements", "--set", which, "--db", str(tmp_path / "t.db")])
