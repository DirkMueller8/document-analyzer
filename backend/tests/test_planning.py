from datetime import date
from pathlib import Path

import pytest

from planning.week import load_goals, main, parse_plan, parse_value, report

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "goals.example.yaml"

PLAN = """# Daily plan

## 2026-11-02
Plan:
- G1: easy run (40 min)
G1 = 40 = not a value
Balance:
G1 = 40
G2 = ja
G3 = 2,5
Caught = 3
Mood = +1
Lesson: start slower.
Pain: calf after the hills.

## 03.11.2026
G1 = 60 min
G2 = nein
G3 = 1
Caught = 5
Mood = −2

## 2026-11-04
Plan only, no balance yet.

## 2026-11-05
G1 = viel
Unknown = 3
Note: not a configured note.
"""


@pytest.fixture
def goals():
    return load_goals(EXAMPLE)


def test_values_by_kind():
    assert parse_value("hours", "2,5") == 2.5 and parse_value("hours", "1 h") == 1.0
    assert parse_value("minutes", "60 min") == 60.0 and parse_value("minutes", "-5") is None
    assert parse_value("yesno", "Ja") == 1.0 and parse_value("yesno", "no") == 0.0 and parse_value("yesno", "vielleicht") is None
    assert parse_value("scale", "−2") == -2.0 and parse_value("scale", "+6") is None
    assert parse_value("count", "3") == 3.0 and parse_value("count", "2,5") is None


def test_the_plan_file_is_read_per_day(goals):
    days, problems = parse_plan(PLAN, goals)
    assert [d.day for d in days] == [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)]
    assert days[0].values == {"G1": 40.0, "G2": 1.0, "G3": 2.5, "Caught": 3.0, "Mood": 1.0}
    assert days[0].notes == [("Lesson", "start slower."), ("Pain", "calf after the hills.")]
    assert days[1].values["G1"] == 60.0 and days[1].values["Mood"] == -2.0     # "dd.mm.yyyy" heading too
    assert days[2].values == {} and days[3].values == {}
    # a value that is not understood is named by line and key, never by its text
    assert problems == ["line 6: value of G1 not understood", "line 27: value of G1 not understood"]


def test_weekly_report(goals):
    days, _ = parse_plan(PLAN, goals)
    text = report(days, goals, today=date(2026, 11, 5), weeks=2)
    assert "Week 2026-W45 (02.11.2026 .. 08.11.2026): 2 of 4 days with a balance; missing: 04.11., 05.11." in text
    assert "G1         Half marathon under 2 hours: running minutes: 100 min in all (2 days); " \
           "target at least 180 per week: 80 short" in text
    assert "yes on 1 of 2 days; target at least 2 per week: 1 short" in text
    assert "mean 1.8 h per day (2 days); target at most 2 per day until 31.12.2026: met" in text
    assert "Caught" in text and "8 in all, 4 per day (2 days)" in text
    assert "mean -0.5 (2 days)" in text
    assert "Last 2 weeks" in text and "W44" in text and "W45" in text
    assert "due      30.11.2026" not in text                                       # no milestone that close
    assert "11.: Lesson: start slower." not in text and "02.11. Lesson: start slower." in text
    assert "start slower" not in report(days, goals, today=date(2026, 11, 5), notes=False)


def test_stages_and_milestones(goals):
    days, _ = parse_plan("## 2027-01-04\nG3 = 1,5\n", goals)
    text = report(days, goals, today=date(2027, 1, 4), weeks=1)
    assert "target at most 1 per day until 30.06.2027: 0.5 over" in text             # the next stage applies
    assert "due      31.01.2027 G2 Registration" in text and "OVERDUE" not in text
    assert "OVERDUE  31.01.2027" in report(days, goals, today=date(2027, 2, 2), weeks=1)


def test_missing_files_name_the_vault(tmp_path):
    with pytest.raises(SystemExit, match="vault"):
        main(["--plan", str(tmp_path / "none.md"), "--goals", str(EXAMPLE)])


def test_main_prints_the_report(tmp_path, capsys):
    plan = tmp_path / "plan.md"
    plan.write_text(PLAN, encoding="utf-8")
    main(["--plan", str(plan), "--goals", str(EXAMPLE), "--date", "2026-11-05", "--weeks", "1"])
    out = capsys.readouterr().out
    assert out.startswith("Week 2026-W45") and "Lines not read: line 6" in out
