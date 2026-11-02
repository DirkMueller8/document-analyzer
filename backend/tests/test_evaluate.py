from datetime import date
from pathlib import Path

import pytest
import yaml

from enrich import evaluate
from enrich.evaluate import (Counts, Label, Score, exclusion, is_variant, label_from_yaml, labeled_ids,
                             load_labels, match_people, miss_reason, sample, score)
from ingest.store import connect, sync_entries
from test_enrich import DOMAINS, ENTRIES, FakeModel, e


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "t.db")
    sync_entries(c, ENTRIES)
    return c


def test_counts_and_score_text():
    c = Counts()
    c.add({"a", "b"}, {"b", "c"})
    assert (c.tp, c.fp, c.fn) == (1, 1, 1)
    assert "precision  50 %  recall  50 %  F1  50 %" in str(c)
    s = Score()
    s.add(Label(mood=3, people={"lena"}), Label(mood=1, people={"lena", "tom"}))
    s.add(Label(mood=-2), Label(mood=None))
    s.add(Label(mood=None), Label(mood=4))
    assert (s.mood_both, s.mood_abs_error, s.mood_sign_agree, s.mood_missed, s.mood_extra) == (1, 2, 1, 1, 1)
    assert "mean error 2.0 points, same sign 100 %" in str(s) and "model -2.0 points on average" in str(s)
    s.add(Label(mood=0), Label(mood=3))
    assert s.mood_signs == {(1, 1): 1, (0, 1): 1}
    assert "  you      zero:         0     0         1" in str(s)
    assert str(Score()).count(" - ") >= 4                       # nothing to divide by: no crash
    by_len = Score()
    by_len.add(Label(mood=2), Label(mood=2), length=200)
    by_len.add(Label(mood=3), Label(mood=0), length=5000)
    assert by_len.mood_by_length == {"short": [1, 0, 1], "long": [1, 3, 0]}
    assert "  long entries (over 1500 characters): 1, mean error 3.0, same sign 0 %" in str(by_len)


def test_baseline_and_per_value_lines():
    s = Score()
    for truth, pred in ((3, 1), (3, 0), (2, 2), (-1, -2), (0, 0)):
        s.add(Label(mood=truth), Label(mood=pred))
    text = "\n".join(s.mood_lines())
    assert "always guessing 'positive' (your most frequent sign) would be right in 60 %" in text
    assert "you rated +3: 2 entries, model average +0.5" in text and "you rated 0: 1 entry, model average +0.0" in text
    assert "you rated -1: 1 entry, model average -2.0" in text


def test_breakdowns_explain_the_errors():
    s = Score()
    pred = Label(mood=0, people={"jonas"}, domains={"endurance", "strength"}, health=True,
                 milestones={"achievement"}, health_categories={"other"},
                 dropped={"petrov": "public_or_watched", "mia": "not_in_text"})
    s.add(Label(mood=0, people={"jonas berg", "petrov", "mia", "tom"}, domains={"endurance"},
                milestones={"first"}), pred)
    s.add(Label(mood=0, milestones={"setback"}), Label(mood=0))
    assert (s.people.tp, s.people_variants) == (1, 1)                   # "jonas" ~ "jonas berg" counts as right
    assert s.people_missed == {"marked public or only watched": 1, "removed by the text check": 1, "not found": 1}
    assert (s.per_domain["endurance"].tp, s.per_domain["strength"].fp) == (1, 1)
    assert s.health_extra == {"other": 1}
    assert s.milestone_entries == {"both, different kinds": 1, "only you": 1}
    text = str(s)
    assert "  strength: 0 right, 1 extra, 0 missed" in text and "extra reports by category: other 1" in text


def test_miss_reasons_for_variants():
    pred = Label(people={"lena berg"}, dropped={"tom kurz": "not_a_person", "mia": "not_in_text"})
    assert miss_reason("tom", pred) == "a variant was marked not a person"
    assert miss_reason("mia", pred) == "removed by the text check"
    assert miss_reason("lena", pred) == "variant of a name already matched to another person"
    assert miss_reason("jonas", pred) == "not found"


def test_name_variants():
    assert is_variant("lena", "lena berg") and is_variant("lena b.", "lena berg") and is_variant("berg", "lena berg")
    assert not is_variant("lena", "lenny") and not is_variant("tom", "lena berg")
    assert match_people({"lena", "tom"}, {"lena berg", "mia"}) == (0, 1, {"tom"}, {"mia"})
    assert match_people({"lena"}, {"lena", "lena berg"}) == (1, 0, set(), {"lena berg"})  # exact first


def test_label_parsing_is_forgiving():
    lab = label_from_yaml({"mood": "", "people": [" Lena ", "lena"], "domains": None, "health": "yes",
                           "milestones": ["first"]})
    assert lab == Label(None, {"lena"}, set(), True, {"first"})
    assert label_from_yaml({"mood": -3}).mood == -3
    assert label_from_yaml({"mood": "+3"}).mood == 3
    plain = label_from_yaml({"people": "Lena, Tom", "domains": "endurance", "milestones": "first, setback"})
    assert (plain.people, plain.domains, plain.milestones) == ({"lena", "tom"}, {"endurance"}, {"first", "setback"})


def test_sample_writes_readable_labels_with_text(con):
    text = sample(con, 2, seed=1, domains=DOMAINS)
    assert text.startswith("# Labels") and "endurance, strength" in text
    items = yaml.safe_load(text)
    assert len(items) == 2 and all(it["done"] is False and it["text"] for it in items)
    assert sample(con, 2, seed=1, domains=DOMAINS) == text            # same seed, same sample
    ranged = yaml.safe_load(sample(con, 5, seed=1, date_from=date(2021, 1, 1), domains=DOMAINS))
    assert [it["date"] for it in ranged] == ["05.03.2021"]


def test_score_uses_done_entries_and_detects_edits(con, tmp_path):
    items = yaml.safe_load(sample(con, 3, seed=1, domains=DOMAINS))
    truth = {"03.04.2005": {"mood": 4, "people": ["Jonas"], "domains": ["endurance"]},
             "10.04.2005": {"mood": 5, "domains": ["endurance"], "health": True, "milestones": ["first"]},
             "05.03.2021": {"people": ["Jonas"], "domains": ["strength"]}}
    for it in items:
        it.update(truth[it["date"]], done=True)
    items[2]["done"] = False
    result, skipped = score(con, items, FakeModel(), DOMAINS)
    assert result.entries == 2 and skipped["not done"] == 1
    assert (result.people.tp, result.people.fp) == (1, 0)             # Mia was dropped by the text check
    assert (result.domains.tp, result.milestones.tp, result.health.tp) == (2, 1, 1)
    assert result.mood_abs_error == 0
    details: list[str] = []
    score(con, items, FakeModel(), DOMAINS, details=details)
    assert details == []                                        # labels match the model: nothing listed
    items[0].update(mood=1, health=True, people=["Jonas", "Tom"])
    score(con, items, FakeModel(), DOMAINS, details=details)
    assert details[0] == "[03.04.2005] (id 1)"
    assert details[1].startswith("    mood: you +1, model +4")
    assert details[2] == "    people: model extra -, missed ['tom']  [tom: not found]"
    assert details[3] == "    health: you yes, model no"
    sync_entries(con, [e(date(2005, 4, 3), "Radtour, anders.", 1)] + ENTRIES[1:])
    _, skipped = score(con, items, FakeModel(), DOMAINS)
    assert skipped["changed or removed"] == 1


def test_new_sample_skips_entries_of_other_label_files(con, tmp_path):
    first = yaml.safe_load(sample(con, 2, seed=1, domains=DOMAINS))
    (tmp_path / "enrich_labels.yaml").write_text(yaml.safe_dump(first), encoding="utf-8")
    (tmp_path / "notes.yaml").write_text("- {id: 4}\n", encoding="utf-8")               # not a label file: ignored
    exclude = labeled_ids(tmp_path, tmp_path / "enrich_labels_validation.yaml")
    assert exclude == {it["id"] for it in first}
    assert labeled_ids(tmp_path, tmp_path / "enrich_labels.yaml") == set()                # never excludes itself
    fresh = yaml.safe_load(sample(con, 5, seed=2, domains=DOMAINS, exclude=exclude))
    assert len(fresh) == 1 and not {it["id"] for it in fresh} & exclude                   # 1 of the 3 entries is left


def test_edited_entries_do_not_slip_into_a_fresh_sample(con, tmp_path):
    items = yaml.safe_load(sample(con, 3, seed=1, domains=DOMAINS))
    assert exclusion(con, items) == ({it["id"] for it in items}, 0, 0)         # nothing edited yet
    sync_entries(con, [e(date(2005, 4, 3), "Radtour, korrigierter Text.", 1)] + ENTRIES[1:])
    ids, stale, by_date = exclusion(con, items)
    assert stale == 1 and by_date == 1                                         # the new version has a new id
    new_id = con.execute("SELECT id FROM entries WHERE raw_date = '03.04.2005'").fetchone()[0]
    assert new_id in ids and new_id not in {it["id"] for it in items}
    assert yaml.safe_load(sample(con, 5, seed=2, domains=DOMAINS, exclude=ids)) == []   # all 3 entries are covered


def test_relative_paths_are_taken_from_the_repo_root(tmp_path):
    root = evaluate.DATA.parent
    assert evaluate.repo_path(Path("data/enrich_labels_validation.yaml")) == root / "data/enrich_labels_validation.yaml"
    assert evaluate.repo_path(tmp_path / "x.yaml") == tmp_path / "x.yaml"                # absolute stays as it is


def test_sample_never_overwrites_labels(con, tmp_path, monkeypatch):
    labels = tmp_path / "labels.yaml"
    labels.write_text("- {id: 1, done: true}\n", encoding="utf-8")
    db = tmp_path / "t.db"
    with pytest.raises(SystemExit, match="not overwritten"):
        evaluate.main(["sample", "--db", str(db), "--labels", str(labels)])
    assert load_labels(labels) == [{"id": 1, "done": True}]
