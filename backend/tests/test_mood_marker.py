from datetime import date

import pytest

from analysis.measurements import (MOOD_KIND, count_ignored_variants, count_unstorable, extract, load_kinds,
                                    mood_variant_shapes, store_all)
from enrich.evaluate import score_mood
from enrich.extract import extract_entry, strip_markers, system_prompt
from enrich.run import enrich
from enrich.schema import build_model
from ingest.store import connect, sync_entries
from test_enrich import DOMAINS, FakeModel, e

DAY = date(2021, 3, 5)


def moods(text: str) -> list[float]:
    return [m.value for m in extract(text, DAY, [MOOD_KIND])]


def test_the_exact_format_is_read():
    assert moods("Stimmung = +2") == [2.0] and moods("Stimmung = -3") == [-3.0] and moods("Stimmung = 0") == [0.0]
    assert moods("stimmung=+1") == [1.0] and moods("  Mood = 5  ") == [5.0]
    assert moods("Stimmung = −3") == [-3.0] and moods("Stimmung = –2") == [-2.0]   # typographic minus
    assert moods("12.03.2021\nStimmung = +2\nLauf mit Lena.") == [2.0]                          # on its own line


def test_everything_else_is_a_counted_near_miss_and_never_stored():
    for line in ("Stimmung = 7", "Stimmung = +2,5", "Stimmung: 2", "Heute Stimmung = +2", "Stimmung = 3x",
                 "Stimmung = 3-4", "Stimmung = 3/5", "Stimmung = 20", "Stimmung = 2.5"):
        assert moods(line) == [], line
        assert count_ignored_variants(line, [MOOD_KIND]) == {"mood": 1}, line
    assert count_ignored_variants("Die Stimmung war gut.", [MOOD_KIND]) == {}        # ordinary prose is not a variant


def test_diary_text_may_follow_the_value_on_the_same_line():
    assert moods("Stimmung = +2 Heute war ein guter Tag.") == [2.0]
    assert moods("Stimmung = -3. Mueder Tag.") == [-3.0] and moods("Stimmung = 0; Ruhetag") == [0.0]
    assert moods("Stimmung = + 2 Lauf") == [2.0] and moods("Stimmung = - 3") == [-3.0]       # space after the sign
    assert moods("Stimmung = 1 (eher ruhig)") == [1.0]
    # the marker alone is hidden from the model, the diary text on its line stays visible
    assert strip_markers("Stimmung = +2 Heute war ein guter Tag.\nLauf.") == "Heute war ein guter Tag.\nLauf."
    assert strip_markers("Stimmung = -3.\nLauf.") == "Lauf." and strip_markers("Stimmung: 2,5 Lauf") == "Lauf"
    assert strip_markers("Stimmung = 3/5 Lauf") == "Lauf" and strip_markers("Stimmung = 3-4 Lauf") == "Lauf"


def test_markers_in_table_rows_are_read_across_cell_boundaries():
    # a table row joins its cells with a tab
    assert moods("Stimmung = -1\tNotiz in der dritten Spalte") == [-1.0]
    assert moods("Lauf mit Lena\tStimmung = 3") == [3.0] and moods("Notiz\tStimmung = 0\tRest") == [0.0]
    assert moods("Lauf Stimmung = 2\tRest") == [] and moods("Stimmung = 2 Lauf\tRest") == [2.0]   # edge of a cell
    assert strip_markers("Notiz\tStimmung = 3\tRest") == "Notiz\tRest"
    assert strip_markers("Stimmung = -1\tNotiz") == "Notiz"
    assert strip_markers("Notiz\tStimmung = 3") == "Notiz\t"


def test_unread_lines_are_classified_by_shape():
    text = "\n".join([
        "Stimmung = +2",                 # read: not counted
        "Stimmung: 2", "Stimmung 3", "Stimmung = 2,5", "Stimmung = 8", "Stimmung = 10",
        "Stimmung = 3x", "Stimmung = 3/5", "Heute Stimmung = 2", "Stimmung (3)", "Die Stimmung war gut.",
    ])
    assert mood_variant_shapes(text) == {
        "colon instead of '='": 1, "no '=' sign": 1, "decimal value": 1, "value outside -5..+5": 2,
        "text after the value": 2, "text before the marker": 1,
        "other characters between the word and the value": 1}
    assert mood_variant_shapes("Ruhetag") == {}


def test_markers_on_entries_without_a_day_are_counted_not_stored():
    assert count_unstorable(["Erster Lauf.\nStimmung = 0", "Ruhetag", "Stimmung = -1\nStimmung = 2"], [MOOD_KIND]) == 3
    assert count_unstorable(["Ruhetag"], [MOOD_KIND]) == 0


def test_built_in_unless_the_config_defines_its_own(tmp_path):
    assert [k.name for k in load_kinds(tmp_path / "missing.yaml")] == ["mood", "achievement"]
    cfg = tmp_path / "m.yaml"
    cfg.write_text("measurements:\n  weight:\n    unit: kg\n    patterns: ['(?P<value>\\d+) kg']\n", encoding="utf-8")
    assert [k.name for k in load_kinds(cfg)] == ["weight", "mood", "achievement"]
    cfg.write_text("measurements:\n  mood:\n    min: -5\n    max: 5\n    patterns: ['^M (?P<value>[+-]?\\d)$']\n",
                   encoding="utf-8")
    kinds = load_kinds(cfg)
    assert [k.name for k in kinds] == ["mood", "achievement"]            # the config's own mood replaces the built-in
    assert extract("M +3", DAY, kinds)[0].value == 3.0 and not extract("Stimmung = 3", DAY, kinds)


def test_the_model_never_sees_the_marker():
    text = "Lauf mit Lena.\nStimmung = +2\nStimmung: 3\nSpäter Kaffee.\n"
    assert strip_markers(text) == "Lauf mit Lena.\nSpäter Kaffee.\n"
    assert strip_markers("Die Stimmung war gut, Stimmung = so la la.") == "Die Stimmung war gut, Stimmung = so la la."
    seen = []

    def chat(messages, schema):
        seen.append(messages[-1]["content"])
        return FakeModel()(messages, schema)

    extract_entry(text, "05.03.2021", chat, build_model(["endurance"]), system_prompt(DOMAINS[:1]))
    assert seen and all("Stimmung" not in s and "+2" not in s for s in seen)


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "t.db")
    sync_entries(c, [
        e(date(2005, 4, 3), "Radtour mit Jonas, 2 Stunden.\nStimmung = +4", 1),
        e(date(2005, 4, 10), "Erster Halbmarathon, 1:52 h.", 2),
        e(date(2021, 3, 5), "Krafttraining 45 Minuten.\nStimmung = -2", 3),
        e(date(2021, 3, 6), "Krafttraining 30 Minuten.\nStimmung = 1\nStimmung = 2", 4),
    ])
    store_all(c, [MOOD_KIND])
    return c


def test_view_prefers_the_rated_value_and_names_the_source(con):
    rows = lambda: con.execute("SELECT date_start, mood, source FROM entry_mood ORDER BY entry_id").fetchall()  # noqa: E731
    assert rows() == [("2005-04-03", 4.0, "rated"), ("2005-04-10", None, None), ("2021-03-05", -2.0, "rated"),
                      ("2021-03-06", 1.5, "rated")]                       # two ratings in one entry: their mean
    enrich(con, FakeModel(), DOMAINS, "fake")                             # the model's moods: 4 / 5 / 0 / 0
    assert rows() == [("2005-04-03", 4.0, "rated"), ("2005-04-10", 5, "estimated"),
                      ("2021-03-05", -2.0, "rated"), ("2021-03-06", 1.5, "rated")]


def test_score_mood_compares_with_the_rated_values(con):
    result, skipped = score_mood(con, FakeModel(), DOMAINS)
    assert result.entries == 2 and result.mood_both == 2                  # the entry with two ratings is skipped
    assert result.mood_abs_error == 2 and result.mood_bias == 2           # model: +4 (right) and 0 where you rated -2
    assert result.mood_signs == {(1, 1): 1, (-1, 0): 1}
    assert skipped == {"several ratings in one entry": 1, "failed": 0}
    assert "same sign 50 % (2 entries)" in "\n".join(result.mood_lines())
    assert score_mood(con, FakeModel(), DOMAINS, limit=1)[0].entries == 1
