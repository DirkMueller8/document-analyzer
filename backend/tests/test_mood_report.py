from datetime import date

import pytest

import math

from analysis.measurements import MOOD_KIND, store_all
from analysis.mood_report import (Period, adjusted_estimates, build_report, change, conclusions, load, main, period_key,
                                  rated_check, render_adjusted, render_check, render_table, summarize)
from enrich.run import store_mood
from ingest.segment import Entry
from ingest.store import connect, sync_entries
from test_enrich import e


def add_year(entries, year, moods):
    entries += [e(date(year, 1 + i % 12, 1 + i // 12), f"Eintrag {year}-{i}", len(entries) + 1) for i in range(len(moods))]


@pytest.fixture
def con(tmp_path):
    entries, moods = [], []
    for year, values in ((2008, [-3, -2, -2, -3, -1, -2, -3, -2, -2, -1, -3, -2]),   # clearly low
                         (2009, [1, 2, 1, 0, 2, 1, 1, 2, 1, 1, 2, 1]),               # clearly higher
                         (2010, [3, -3]),                                             # too few entries
                         (2011, [-2, 3, -3, 2, 1, -1, 0, 2, -2, 3, -3, 1])):         # wide spread
        add_year(entries, year, values)
        moods += values
    entries.append(Entry(date(2008, 1, 1), date(2008, 12, 31), "year", "2008", 99, "plain", ["Nur das Jahr bekannt."]))
    moods.append(-5)
    c = connect(tmp_path / "t.db")
    sync_entries(c, entries)
    ids = c.execute("SELECT id, text_hash FROM entries ORDER BY source_pos").fetchall()
    for (entry_id, h), mood in zip(ids, moods):
        store_mood(c, entry_id, h, mood, "fake", "mood1:x")
    return c


def test_period_keys():
    assert [period_key("2008-08-15", by) for by in ("year", "half", "quarter", "month")] == [
        "2008", "2008 H2", "2008 Q3", "2008-08"]
    assert period_key("2008-06-30", "half") == "2008 H1" and period_key("2008-01-01", "quarter") == "2008 Q1"


def test_an_entry_known_only_by_its_year_counts_for_years_only(con):
    assert len(load(con, "year")[0]) == 39 and load(con, "year")[1] == 39
    rows, dated = load(con, "quarter")
    assert len(rows) == 38 == dated                                                 # the year-only entry is left out
    assert len(load(con, "year", date_from=date(2009, 1, 1), date_to=date(2009, 12, 31))[0]) == 12


def test_means_intervals_and_shares(con):
    periods = {p.key: p for p in summarize(load(con, "year")[0])}
    p = periods["2009"]
    assert (p.n, p.rated) == (12, 0) and p.mean == pytest.approx(15 / 12)
    assert p.low < p.mean < p.high and (p.high - p.low) < 1.0
    assert (p.negative, p.zero, p.positive) == (0.0, pytest.approx(1 / 12), pytest.approx(11 / 12))
    assert periods["2010"].n == 2 and periods["2010"].low is not None
    single = summarize([("1999", 2.0, "estimated")])[0]
    assert single.low is None and single.mean == 2.0                                # one entry: no interval
    assert periods["2008"].n == 13                                                  # twelve days plus the year-only entry


def test_a_change_is_only_named_when_the_intervals_are_apart(con):
    periods = summarize(load(con, "year")[0])
    by_key = {p.key: p for p in periods}
    assert change(by_key["2008"], by_key["2009"], 10) == "higher"
    assert change(by_key["2009"], by_key["2008"], 10) == "lower"
    assert change(by_key["2009"], by_key["2011"], 10) == "same"                     # 2011 varies too much
    assert change(by_key["2009"], by_key["2010"], 10) == ""                         # 2010 has too few entries
    assert change(None, by_key["2009"], 10) == ""


def test_table_and_conclusions(con):
    periods = summarize(load(con, "year")[0])
    table = render_table(periods, 10)
    assert "2010" in table and "* fewer than 10 entries: shown, not interpreted" in table
    row = lambda year: next(line for line in table.splitlines() if line.startswith(year))  # noqa: E731
    assert row("2009").rstrip().endswith("higher than 2008") and "+1.2" in row("2009")
    assert row("2010").rstrip().endswith("*") is False and row("2010").rstrip().endswith("2009") is False  # not compared
    assert row("2011").rstrip().endswith("same as 2009")                  # the thin year 2010 is skipped as reference
    text = "\n".join(conclusions(periods, 10))
    assert "highest period 2009" in text and "lowest 2008" in text and "difference is clear" in text
    assert conclusions(periods[:1], 10)[0].startswith("Fewer than two periods")


def test_the_rated_value_wins_and_is_counted(con):
    first = con.execute("SELECT id FROM entries WHERE date_start = '2009-01-01'").fetchone()[0]
    con.execute("UPDATE entries SET text = text || char(10) || 'Stimmung = -4' WHERE id = ?", (first,))
    store_all(con, [MOOD_KIND])
    periods = {p.key: p for p in summarize(load(con, "year")[0])}
    assert periods["2009"].rated == 1 and periods["2009"].mean == pytest.approx((15 - 1 - 4) / 12)


def test_check_against_your_own_ratings(con):
    assert render_check(rated_check(con, "year")) == "" and "Check against" not in build_report(con, "year", 10)[0]
    ids = [r[0] for r in con.execute("SELECT id FROM entries WHERE date_start LIKE '2009-%' ORDER BY id")][:4]
    for i in ids:                                                       # rate four days of 2009 with -4
        con.execute("UPDATE entries SET text = text || char(10) || 'Stimmung = -4' WHERE id = ?", (i,))
    first = con.execute("SELECT id FROM entries WHERE date_start LIKE '2008-%' ORDER BY id").fetchone()[0]
    con.execute("UPDATE entries SET text = text || char(10) || 'Stimmung = -3' WHERE id = ?", (first,))
    store_all(con, [MOOD_KIND])
    rows = rated_check(con, "year")
    assert rows == [("2008", 1, -3.0, -3.0), ("2009", 4, -4.0, 1.0)]       # the model's own estimates: -3 | 1, 2, 1, 0
    text = render_check(rows)
    assert "2009" in text.split("\n")[2] and "+5.0" in text.split("\n")[2]  # model - you; 2008 has only one entry
    assert "all periods: 5 entries, the model is +4.0 points above" in text
    assert "Check against your own ratings" in build_report(con, "year", 10)[0]


def test_the_mean_of_a_period_is_corrected_by_the_rated_days(tmp_path):
    c = connect(tmp_path / "adj.db")
    sync_entries(c, [e(date(2009, 1 + i // 3, 1 + i % 3), f"Eintrag {i}" + (f"\nStimmung = {r}" if r is not None else ""),
                       i + 1)
                     for i, r in enumerate([-1, -2, -3, -2, -2, None, None, None, None, None])])
    store_all(c, [MOOD_KIND])
    for (entry_id, h), model in zip(c.execute("SELECT id, text_hash FROM entries ORDER BY source_pos").fetchall(),
                                    [0, 0, 0, 0, 0, 2, 2, 2, 2, 2]):
        store_mood(c, entry_id, h, model, "fake", "mood1:x")
    (a,) = adjusted_estimates(c, "year")
    # estimates: five 0 and five 2 -> mean 1.0, variance 10/9; corrections -1 -2 -3 -2 -2 -> mean -2.0, variance 0.5
    assert (a.key, a.rated) == ("2009", 5) and a.estimates == pytest.approx(1.0) and a.correction == pytest.approx(-2.0)
    assert a.value == pytest.approx(-1.0)
    half = 2.776 * math.sqrt((10 / 9) / 10 + 0.5 / 5)                     # t for 4 degrees of freedom
    assert (a.low, a.high) == pytest.approx((-1.0 - half, -1.0 + half))
    assert adjusted_estimates(c, "year", min_rated=6) == []               # too few rated days: no correction
    text = render_adjusted([a])
    assert "2009" in text.splitlines()[2] and "-1.0" in text.splitlines()[2] and "[-2.3, +0.3]" in text
    assert "Corrected by your rated days" in build_report(c, "year", 1)[0]
    assert render_adjusted([]) == ""


def test_report_text_and_empty_database(con, tmp_path):
    text, periods = build_report(con, "year", 10)
    assert text.startswith("Mood by year: 39 of 39 dated entries have a mood (0 rated by you, 39 estimated")
    assert "Please note:" in text and "Entries are the days you wrote about" in text
    empty = connect(tmp_path / "empty.db")
    message, none = build_report(empty, "year", 10)
    assert none == [] and "--mood-only" in message


def test_a_chart_that_cannot_be_replaced_is_written_under_a_new_name(con, tmp_path, capsys):
    db = tmp_path / "t.db"
    con.close()
    blocked = tmp_path / "mood.png"
    blocked.mkdir()                                     # stands in for a file that is locked by a viewer
    main(["--db", str(db), "--chart", str(blocked), "--no-notes"])
    out = capsys.readouterr().out
    written = [p for p in tmp_path.iterdir() if p.name.startswith("mood_") and p.suffix == ".png"]
    assert len(written) == 1 and written[0].stat().st_size > 5000 and not list(tmp_path.glob("*.tmp"))
    assert f"Chart written to {written[0]}" in out and "could not be replaced" in out


def test_an_unwritable_folder_gives_a_clear_message_not_a_traceback(con, tmp_path, capsys):
    db = tmp_path / "t.db"
    con.close()
    with pytest.raises(SystemExit, match="The table above is complete"):
        main(["--db", str(db), "--chart", str(tmp_path / "missing_folder" / "mood.png"), "--no-notes"])
    assert "Mood by year" in capsys.readouterr().out          # the table was printed before


def test_the_command_writes_a_chart(con, tmp_path, capsys):
    db = tmp_path / "t.db"
    con.close()
    png = tmp_path / "mood.png"
    main(["--db", str(db), "--chart", str(png), "--by", "year", "--no-notes"])
    out = capsys.readouterr().out
    assert "Mood by year" in out and f"Chart written to {png}" in out
    assert png.exists() and png.stat().st_size > 5000
    main(["--db", str(db), "--by", "quarter", "--no-chart", "--from", "2009", "--to", "2009", "--no-notes"])
    assert "2009 Q1" in capsys.readouterr().out and not (tmp_path / "mood_by_quarter.png").exists()
