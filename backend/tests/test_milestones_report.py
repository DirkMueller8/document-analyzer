from datetime import date

import pytest

from analysis.measurements import load_kinds, store_all
from analysis.milestones_report import domain_rows, main, period_rows, render
from ingest.store import connect, sync_entries
from test_enrich import e


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "t.db")
    sync_entries(c, [
        e(date(2005, 4, 3), "Achievement = 2: Lauf-Trainerschein bestanden.", 1),
        e(date(2005, 4, 10), "Erster Halbmarathon, danach Knie verletzt.", 2),
        e(date(2005, 9, 1), "Ruhetag.", 3),
        e(date(2006, 2, 5), "Neuer Radverein, erstes Training.", 4),
        e(date(2006, 3, 5), "Zum ersten Mal Schwimmtraining.", 5),
    ])
    store_all(c, load_kinds(tmp_path / "missing.yaml"))
    ids = {d: i for i, d in c.execute("SELECT id, date_start FROM entries")}
    with c:
        for d, mood in (("2005-04-03", 3), ("2005-04-10", -1), ("2005-09-01", 0), ("2006-02-05", 2), ("2006-03-05", 1)):
            c.execute("INSERT INTO entry_meta (entry_id, text_hash, mood_score) VALUES (?, 'h', ?)", (ids[d], mood))
        c.executemany("INSERT INTO milestones (entry_id, domain, title, kind) VALUES (?, ?, 'Titel', ?)", [
            (ids["2005-04-03"], "endurance", "achievement"),
            (ids["2005-04-03"], "endurance", "achievement"),     # twice in one entry: the entry counts once
            (ids["2005-04-10"], "endurance", "first"),
            (ids["2005-04-10"], "endurance", "setback"),
            (ids["2006-02-05"], "cycling", "first"),
            (ids["2006-02-05"], "cycling", "decision"),
            (ids["2006-03-05"], "swimming", "first"),
        ])
    return c


def test_rows_per_year(con):
    rows = {r.key: r for r in period_rows(con)}
    y5, y6 = rows["2005"], rows["2006"]
    assert (y5.entries, y5.marked) == (3, 1) and y5.by_kind == {"achievement": 1, "first": 1, "setback": 1, "decision": 0}
    assert y5.mood == pytest.approx(2 / 3)                                      # (3 - 1 + 0) / 3
    assert y6.by_kind["first"] == 2 and y6.starts_per_achievement() is None
    assert y5.starts_per_achievement() == 1.0


def test_domains_and_render(con):
    domains = domain_rows(con)
    assert [d for d, _ in domains] == ["endurance", "cycling", "swimming"]       # most achievements first
    text = render(period_rows(con), domains, min_n=2)
    assert "2005" in text and "2006" in text and "Titel" not in text and "Trainerschein" not in text
    assert "most achievements per entry: 2005 (33 %), 2006 (0 %)" in text
    assert "most starts per achievement: 2005 (1.0)" in text
    assert "trend, not a count" in text
    assert "(few entries)" in render(period_rows(con), domains, min_n=5)


def test_main_needs_milestones(tmp_path):
    db = tmp_path / "empty.db"
    connect(db).close()
    with pytest.raises(SystemExit, match="enrich"):
        main(["--db", str(db)])
