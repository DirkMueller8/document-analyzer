from datetime import date, timedelta

import pytest

from analysis.measurements import extract, load_kinds, store_all
from analysis.weight import jumps, load_series, longest_streak, outliers, slope_per_year, summarize, Point
from ingest.segment import Entry
from ingest.store import connect, sync_entries

CONFIG = r"""
measurements:
  weight:
    unit: kg
    min: 30
    max: 250
    patterns:
      - '\bW\s*[=:]\s*(?P<value>\d{2,3}(?:[.,]\d{1,2})?)\s*(?:kg\b)?'
      - '(?i)\bgewicht\s*[:=]\s*(?P<value>\d{2,3}(?:[.,]\d{1,2})?)\s*kg\b'
"""


@pytest.fixture
def kinds(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text(CONFIG, encoding="utf-8")
    return load_kinds(p)


def test_value_on_entry_day(kinds):
    ms = extract("Guter Tag. W = 85,5 kg, danach Sport.", date(2024, 3, 12), kinds)
    assert [(m.date, m.value, m.source) for m in ms] == [(date(2024, 3, 12), 85.5, "entry")]


def test_variants_and_first_pattern_wins(kinds):
    assert [m.value for m in extract("W=84.3kg", date(2024, 1, 1), kinds)] == [84.3]
    assert [m.value for m in extract("W: 84", date(2024, 1, 1), kinds)] == [84.0]
    assert [m.value for m in extract("Gewicht: 83,9 kg", date(2024, 1, 1), kinds)] == [83.9]
    assert extract("Wetter = schön, WW = 12", date(2024, 1, 1), kinds) == []


def test_reference_lines_use_their_own_date(kinds):
    text = "Gewichtsverlauf:\n10.03 W = 86,0 kg\n11.03. W = 85,8\nRef_05.01.2023 W = 90,1 kg"
    ms = extract(text, date(2024, 3, 12), kinds)
    assert [(m.date, m.value, m.source) for m in ms] == [
        (date(2024, 3, 10), 86.0, "ref"), (date(2024, 3, 11), 85.8, "ref"), (date(2023, 1, 5), 90.1, "ref")]


def test_day_month_after_entry_date_means_previous_year(kinds):
    ms = extract("28.12 W = 88,0", date(2024, 1, 3), kinds)
    assert ms[0].date == date(2023, 12, 28)


def test_implausible_values_ignored(kinds):
    assert extract("W = 12 kg und W = 999", date(2024, 1, 1), kinds) == []


def test_store_and_series_prefers_same_day_value(kinds, tmp_path):
    con = connect(tmp_path / "t.db")
    e1 = Entry(date(2024, 3, 11), date(2024, 3, 11), "day", "11.03.2024", 1, "plain", ["W = 86,0"])
    e2 = Entry(date(2024, 3, 12), date(2024, 3, 12), "day", "12.03.2024", 2, "plain",
               ["W = 85,5\n11.03 W = 99,9"])  # later reference to the 11th loses
    month = Entry(date(2024, 4, 1), date(2024, 4, 30), "month", "April 2024", 3, "plain", ["W = 80"])
    sync_entries(con, [e1, e2, month])
    assert store_all(con, kinds) == 3  # month-precision entry skipped
    assert [(p.day, p.value) for p in load_series(con)] == [(date(2024, 3, 11), 86.0), (date(2024, 3, 12), 85.5)]


def test_statistics():
    start = date(2025, 1, 1)
    series = [Point(start + timedelta(days=i), 90 - i * 0.02) for i in range(365)]
    series[100] = Point(series[100].day, 58.5)  # typo
    assert slope_per_year([p for i, p in enumerate(series) if i != 100]) == pytest.approx(-7.3, abs=0.1)
    assert longest_streak(series)[0] == 365
    assert len(jumps(series)) == 2
    assert [p.value for p, _ in outliers(series)] == [58.5]
    text = summarize(series, today=date(2026, 1, 1))
    assert "Measurements:      364 days" in text and "Likely typos" in text and "58.5" in text
    assert "Lowest / highest:  82.7" in text  # the typo no longer counts as minimum


def test_trend_needs_enough_data():
    few = [Point(date(2013, 1, 1) + timedelta(days=30 * i), 70 + i) for i in range(11)]
    assert slope_per_year(few) is None
