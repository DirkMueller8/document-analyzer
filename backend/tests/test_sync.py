from datetime import date

from ingest.segment import Entry
from ingest.store import connect, sync_entries


def entry(day: int, text: str) -> Entry:
    return Entry(date(2024, 1, day), date(2024, 1, day), "day", f"{day}.1.2024", day, "plain", [text])


def ids(con):
    return dict(con.execute("SELECT text, id FROM entries"))


def test_incremental_sync(tmp_path):
    con = connect(tmp_path / "t.db")
    s = sync_entries(con, [entry(1, "Eins"), entry(2, "Zwei")])
    assert (s.kept, s.added, s.removed) == (0, 2, 0)
    before = ids(con)
    con.execute("INSERT INTO entry_meta (entry_id, text_hash, mood_score) VALUES (?, 'x', 3)",
                (before["Eins"],))
    con.execute("INSERT INTO entry_meta (entry_id, text_hash, mood_score) VALUES (?, 'x', 1)",
                (before["Zwei"],))
    con.commit()

    # daily use: entry 2 edited, entry 3 appended
    s = sync_entries(con, [entry(1, "Eins"), entry(2, "Zwei, ergänzt"), entry(3, "Drei")])
    assert (s.kept, s.added, s.removed) == (1, 2, 1)
    after = ids(con)
    assert after["Eins"] == before["Eins"]          # unchanged entry keeps its id ...
    meta = dict(con.execute("SELECT entry_id, mood_score FROM entry_meta"))
    assert meta == {before["Eins"]: 3}              # ... and its enrichment; stale one is gone

    # re-running with no changes touches nothing
    s = sync_entries(con, [entry(1, "Eins"), entry(2, "Zwei, ergänzt"), entry(3, "Drei")])
    assert (s.kept, s.added, s.removed) == (3, 0, 0)


def test_date_fix_does_not_count_as_change(tmp_path):
    con = connect(tmp_path / "t.db")
    sync_entries(con, [entry(1, "Eins")])
    fixed = Entry(date(1999, 1, 1), date(1999, 1, 1), "day", "1.1.1999", 1, "plain", ["Eins"])
    s = sync_entries(con, [fixed])
    assert (s.kept, s.added) == (1, 0)
    assert con.execute("SELECT date_start FROM entries").fetchone()[0] == "1999-01-01"
