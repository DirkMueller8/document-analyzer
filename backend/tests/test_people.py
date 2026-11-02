import json
from datetime import date

import pytest

from enrich.people import PeopleError, _fold, _one_edit, find, ignore, merge, people, rename, suggest, unignore
from enrich.run import enrich, pending, version
from ingest.store import connect, sync_entries
from test_enrich import DOMAINS, FakeModel, e

TEXTS = ["Lauf mit Lena.", "Radtour mit Lena B. und Jörg.", "Training mit Lenchen und Joerg.", "Spiel von Nordhafen."]


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "t.db")
    sync_entries(c, [e(date(2010, 1, i + 1), t, i + 1) for i, t in enumerate(TEXTS)])
    ids = [r[0] for r in c.execute("SELECT id FROM entries ORDER BY id")]
    names = [["Lena"], ["Lena B.", "Jörg"], ["Lenchen", "Joerg"], ["Nordhafen"]]
    with c:
        for entry_id, ns in zip(ids, names):
            for n in ns:
                c.execute("INSERT OR IGNORE INTO people (canonical_name) VALUES (?)", (n,))
                pid = c.execute("SELECT id FROM people WHERE canonical_name = ?", (n,)).fetchone()[0]
                c.execute("INSERT INTO mentions (person_id, entry_id) VALUES (?, ?)", (pid, entry_id))
    return c


def test_list_and_suggest(con):
    ps = {p.name: p for p in people(con)}
    assert ps["Lena"].mentions == 1 and ps["Lena"].first == "2010-01-01"
    pairs = {(a.name, b.name): reason for a, b, reason in suggest(con)}
    assert any({"Jörg", "Joerg"} == set(k) for k in pairs)              # umlaut spelling
    assert any({"Lena", "Lena B."} == set(k) for k in pairs)            # same first name, one with more
    assert not any("Nordhafen" in k for k in pairs)
    assert _fold("Jörg") == _fold("joerg") and _one_edit("lena", "lene") and not _one_edit("lena", "lena")
    assert _one_edit("lena", "lenas") and not _one_edit("lena", "lenchen")


def test_merge_moves_mentions_and_keeps_aliases(con):
    assert merge(con, "Lena", ["Lena B.", "lenchen"]) == 2
    (p,) = [p for p in people(con) if p.name == "Lena"]
    assert p.mentions == 3 and set(p.aliases) == {"Lena B.", "Lenchen"}
    assert find(con, "LENCHEN") == p.id                                 # aliases resolve, any case
    with pytest.raises(PeopleError):
        merge(con, "Lena", ["Lena B."])                                 # already merged into Lena itself
    with pytest.raises(PeopleError, match="No person"):
        merge(con, "Lena", ["Nobody"])


def test_rename_keeps_old_name_as_alias(con):
    rename(con, "Jörg", "Jörg Hansen")
    assert find(con, "Jörg") == find(con, "Jörg Hansen")
    with pytest.raises(PeopleError, match="use merge"):
        rename(con, "Jörg Hansen", "Joerg")


def test_merged_variants_stay_merged_in_later_runs(con):
    merge(con, "Lena", ["Lena B."])
    entries = [e(date(2010, 1, i + 1), t, i + 1) for i, t in enumerate(TEXTS)]
    sync_entries(con, entries + [e(date(2011, 1, 1), "Radtour mit Jonas, 2 Stunden.", 9)])
    con.execute("UPDATE people SET aliases = ? WHERE canonical_name = 'Lena'",
                (json.dumps(["Lena B.", "Jonas"]),))                    # pretend Jonas is a variant of Lena
    con.commit()
    enrich(con, FakeModel(), DOMAINS, "fake", date_from=date(2011, 1, 1))
    assert con.execute("SELECT COUNT(*) FROM people WHERE canonical_name = 'Jonas'").fetchone() == (0,)
    assert [p.mentions for p in people(con) if p.name == "Lena"] == [3]  # 2 merged + the new entry


def test_ignore_removes_now_and_in_future_runs(con):
    assert ignore(con, "Nordhafen") == 1
    assert "Nordhafen" not in {p.name for p in people(con)}
    assert ignore(con, "Jonas") == 0                                   # not a person yet: ignored in advance
    sync_entries(con, [e(date(2005, 4, 3), "Radtour mit Jonas, 2 Stunden.", 9)])
    enrich(con, FakeModel(), DOMAINS, "fake", date_from=date(2005, 1, 1), date_to=date(2005, 12, 31))
    assert con.execute("SELECT COUNT(*) FROM people WHERE canonical_name = 'Jonas'").fetchone() == (0,)
    assert unignore(con, "Jonas") == 1                                 # the entry with "Jonas" is pending again
    assert len(pending(con, version(DOMAINS), date(2005, 1, 1), date(2005, 12, 31))) == 1
    with pytest.raises(PeopleError, match="not on the ignore list"):
        unignore(con, "Jonas")
