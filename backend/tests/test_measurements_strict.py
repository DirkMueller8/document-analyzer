from datetime import date

import pytest

from analysis.measurements import count_ignored_variants, extract, load_kinds

CONFIG = r"""
measurements:
  weight:
    unit: kg
    min: 30
    max: 250
    patterns:
      - '\bG = (?P<value>\d{2,3}[.,]\d) [kK][gG]\b'
    loose_patterns:
      - '\bG\s*[=:]\s*\d{2,3}'
      - '\bG\s+\d{2,3}[.,]\d{1,2}\s*[kK][gG]\b'
"""


@pytest.fixture
def kinds(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text(CONFIG, encoding="utf-8")
    return load_kinds(p)


def values(text, kinds):
    return [m.value for m in extract(text, date(2024, 3, 12), kinds)]


def test_only_the_exact_format_counts(kinds):
    assert values("Heute G = 85,5 kg gemessen.", kinds) == [85.5]
    assert values("G = 85.5 KG", kinds) == [85.5]
    # a bare value is a reference to the past or someone else's weight: ignored
    assert values("Früher waren es 59,0 kg.", kinds) == []
    assert values("Meine Frau wiegt 59,0 kg.", kinds) == []
    # near misses are not stored
    for line in ("G=85,5 kg", "G = 85 kg", "G = 85,5kg", "G: 85,5 kg", "G  =  85,5 kg", "G 85,5 kg"):
        assert values(line, kinds) == [], line


def test_reference_dates_still_apply_to_exact_format(kinds):
    ms = extract("Ref_05.01.2023 G = 90,1 kg", date(2024, 3, 12), kinds)
    assert [(m.date, m.value, m.source) for m in ms] == [(date(2023, 1, 5), 90.1, "ref")]


def test_near_misses_are_counted_not_stored(kinds):
    text = "\n".join([
        "G = 85,5 kg",          # exact: stored, not a near miss
        "G=85,5 kg",            # near miss
        "G = 85 kg",            # near miss (no decimal)
        "G 85,5 kg",            # near miss (no sign)
        "Früher 59,0 kg",       # no G at all: neither stored nor counted
        "Meine Frau: 59,0 kg",  # no G at all
        "Gewicht ist gut",      # not a measurement
    ])
    assert count_ignored_variants(text, kinds) == {"weight": 3}
    assert values(text, kinds) == [85.5]
