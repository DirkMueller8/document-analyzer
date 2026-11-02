import hashlib
import math
import re
from datetime import date

import pytest

from ingest.segment import Entry
from ingest.store import connect, sync_entries
from search import ollama_client
from search.ask import context, dates_in, gather, names_in, parse_when
from search.chunk import chunk_text
from search.hybrid import entries_on, first_mentions, fts_query, nearest_dates, search
from search.index import update_index

DIM = 64


def fake_embed(texts):
    """Deterministic bag-of-words vectors: shared words -> similar vectors (no Ollama needed)."""
    out = []
    for t in texts:
        v = [0.0] * DIM
        for w in re.findall(r"\w+", t.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1
        n = math.sqrt(sum(x * x for x in v)) or 1
        out.append([x / n for x in v])
    return out


def e(d, text, pos):
    return Entry(d, d, "day", d.strftime("%d.%m.%Y"), pos, "plain", [text])


ENTRIES = [
    e(date(1992, 6, 15), "Heute habe ich Lena beim Lauftreff kennengelernt. 10 km locker.", 1),
    e(date(2005, 4, 3), "Radtour 40 Kilometer, 2 Stunden, mittel. Sehr gute Laune.", 2),
    e(date(2005, 4, 10), "Erster Halbmarathon mit dem Laufclub! 1:52 h, intensiv.", 3),
    e(date(2021, 3, 5), "Mit Lena gelaufen, 12 km. Gewicht 76,2 kg, schlafe besser.", 4),
    e(date(2023, 12, 1), "Started rowing, 30 minutes, moderate, first session went well.", 5),
]


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "t.db")
    sync_entries(c, ENTRIES)
    update_index(c, fake_embed, "fake")
    return c


def test_chunking_keeps_paragraphs_and_overlaps():
    assert chunk_text("Kurz.") == ["Kurz."]
    long = "\n".join(f"Absatz {i}. " + "Wort " * 60 for i in range(10))
    chunks = chunk_text(long, target=500, overlap=80)
    assert len(chunks) > 3 and all(len(c) <= 500 + 90 for c in chunks)
    assert chunks[1].startswith("…")
    giant = "Satz eins ist hier. " * 200
    assert all(len(c) <= 1500 + 210 for c in chunk_text(giant))


def test_index_is_incremental_and_follows_deletions(con):
    assert con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 5
    assert update_index(con, fake_embed, "fake") == 0          # nothing new
    sync_entries(con, ENTRIES[:4] + [e(date(2024, 1, 2), "Neuer Eintrag über Intervalltraining.", 6)])
    assert update_index(con, fake_embed, "fake") == 1          # only the new entry
    n_chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert n_chunks == 5
    assert con.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0] == n_chunks   # vector removed too
    assert con.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'rowing'").fetchone()[0] == 0


def test_model_change_rebuilds(con):
    assert update_index(con, fake_embed, "other-model") == 5


def test_hybrid_search_and_date_filter(con):
    hits = search(con, "Wann war der Halbmarathon mit dem Laufclub?", lambda q: fake_embed([q])[0], k=3)
    assert hits[0].raw_date == "10.04.2005" and set(hits[0].sources) == {"fts", "vec"}
    hits = search(con, "Lena", lambda q: fake_embed([q])[0], k=5, date_from=date(2000, 1, 1))
    assert [h.raw_date for h in hits if "Lena" in h.text] == ["05.03.2021"]


def test_first_mentions_is_chronological(con):
    assert [h.raw_date for h in first_mentions(con, "Lena")] == ["15.06.1992", "05.03.2021"]


def test_dates_in_question():
    assert dates_in("Was habe ich am 03.04.2005 gemacht?") == ([date(2005, 4, 3)], None)
    assert dates_in("Und am 3.4.2005 oder 2005-04-10?") == ([date(2005, 4, 3), date(2005, 4, 10)], None)
    assert dates_in("Wie war der 10. April 2005?") == ([date(2005, 4, 10)], None)
    assert dates_in("Was war im Juli 2026 los?") == ([], (date(2026, 7, 1), date(2026, 7, 31)))
    assert dates_in("Am 31.02.2005 und 1:52 h") == ([], None)       # impossible date, no date at all
    assert dates_in("Halbmarathon 2005") == ([], None)              # a bare year is not a filter


def test_named_day_is_looked_up_directly(con, monkeypatch):
    # The date is only metadata, so ranking alone would not find this entry for "was habe ich gemacht".
    monkeypatch.setattr("search.ask.embed", lambda texts: fake_embed(texts))
    hits = gather(con, "Was habe ich am 03.04.2005 gemacht?", k=12, date_from=None, date_to=None)
    assert [h.raw_date for h in hits] == ["03.04.2005"] and hits[0].sources == ["date"]
    assert [h.raw_date for h in entries_on(con, date(2005, 4, 10))] == ["10.04.2005"]
    assert gather(con, "Was war am 04.04.2005?", k=12, date_from=None, date_to=None) == []
    assert nearest_dates(con, date(2005, 4, 4)) == ("03.04.2005", "10.04.2005")


def test_named_month_filters_the_search(con, monkeypatch):
    monkeypatch.setattr("search.ask.embed", lambda texts: fake_embed(texts))
    hits = gather(con, "Was habe ich im April 2005 gemacht?", k=12, date_from=None, date_to=None)
    assert {h.raw_date for h in hits} <= {"03.04.2005", "10.04.2005"} and hits


def test_fts_query_is_safe_and_drops_stopwords():
    assert fts_query('Wann habe ich "Lena" NEAR getroffen*?') == '"Lena" OR "NEAR" OR "getroffen"'
    assert fts_query("wann ich") is None


def test_helpers():
    assert names_in("Wann habe ich Lena zum ersten Mal getroffen?") == ["Lena"]
    assert parse_when("2004-02", end=True) == date(2004, 2, 29)
    assert parse_when("2004") == date(2004, 1, 1)
    assert "[15.06.1992] x" in context([type("H", (), {"raw_date": "15.06.1992", "text": "x"})()])


def test_ollama_guards(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://example.com:11434")
    with pytest.raises(ollama_client.OllamaError, match="loopback"):
        ollama_client.base_url()
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:11434")
    assert ollama_client.base_url() == "http://127.0.0.1:11434"
    with pytest.raises(ollama_client.OllamaError, match="cloud"):
        ollama_client.embed(["x"], model="gpt-oss:120b-cloud")
