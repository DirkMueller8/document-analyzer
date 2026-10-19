from datetime import date

import pytest

from analysis.measurements import count_skipped_lines, extract, load_kinds

CONFIG = r"""
measurements:
  weight:
    unit: kg
    min: 30
    max: 250
    patterns:
      - '\bG = (?P<value>\d{2,3}[.,]\d) [kK][gG]\b'
    skip_lines:
      - '^\s*Ref_'
"""


@pytest.fixture
def kinds(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text(CONFIG, encoding="utf-8")
    return load_kinds(p)


def test_ref_lines_are_excluded_entirely(kinds):
    text = "Heute G = 70,1 kg\nRef_05.01.2023 G = 90,1 kg\n  Ref_03.04 G = 88,0 kg\nFrüher G = 80,0 kg"
    ms = extract(text, date(2024, 3, 12), kinds)
    assert [(m.date, m.value, m.source) for m in ms] == [
        (date(2024, 3, 12), 70.1, "entry"), (date(2024, 3, 12), 80.0, "entry")]


def test_skipped_lines_are_counted_not_stored(kinds):
    text = "Ref_05.01.2023 G = 90,1 kg\nRef_06.01.2023 ohne Messwert\nHeute G = 70,1 kg"
    assert count_skipped_lines(text, kinds) == {"weight": 1}


def test_other_dated_lines_still_use_their_own_date(kinds):
    # only Ref_ is excluded by this config; a bare dd.mm prefix keeps its own date
    ms = extract("10.03 G = 86,0 kg", date(2024, 3, 12), kinds)
    assert [(m.date, m.source) for m in ms] == [(date(2024, 3, 10), "ref")]
