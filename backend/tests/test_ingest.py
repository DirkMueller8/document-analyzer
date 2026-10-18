import sys
from datetime import date
from pathlib import Path

import pytest

from ingest.odt_parser import extract_blocks
from ingest.segment import Entry, Overrides, build_report, segment
from ingest.store import connect, sync_entries

SAMPLES = Path(__file__).resolve().parents[2] / "samples"
sys.path.insert(0, str(SAMPLES))
import make_sample  # noqa: E402


@pytest.fixture(scope="module")
def sample_odt(tmp_path_factory):
    path = tmp_path_factory.mktemp("odt") / "sample.odt"
    make_sample.build(path)
    return path


@pytest.fixture(scope="module")
def blocks(sample_odt):
    return extract_blocks(sample_odt)


def test_table_rows_become_blocks(blocks):
    table = [b for b in blocks if b.kind == "table"]
    assert len(table) == 4
    assert table[1].text.startswith("3.4.2005\tRadtour")


def test_segmentation(blocks):
    entries = segment(blocks)
    dated = [e for e in entries if e.date_start]

    # preamble before the first date is kept as an undated entry
    assert entries[0].date_start is None
    # continuation paragraph is attached to the 1.3.1990 entry
    cold = next(e for e in dated if e.date_start == date(1990, 3, 1))
    assert "Tee und leichte" in cold.text
    # "1989 war ..." is not a date -> appended to the 2025 entry
    assert "1989 war" in dated[-1].text
    # table header row "Datum | Eintrag" is appended to the previous entry, not a new one
    assert sum(e.format == "table" for e in dated) == 3
    assert next(e for e in dated if e.date_start == date(2006, 3, 1)).precision == "month"

    report = build_report(blocks, entries)
    assert report.undated_entries == [entries[0].source_pos]
    assert report.backwards == []
    assert report.precision == {"day": 8, "month": 4, "year": 2}


def test_report_hides_text_by_default(blocks):
    report = build_report(blocks, segment(blocks)).render()
    assert "Trainingstagebuch" not in report and "Lena" not in report


def test_overrides(blocks):
    pre = blocks[0].pos
    entries = segment(blocks, Overrides(dates={pre: "Nov 1989"}))
    assert entries[0].date_start == date(1989, 11, 1)
    entries = segment(blocks, Overrides(skip={pre}))
    assert entries[0].date_start == date(1989, 12, 1)


def test_store_roundtrip(blocks, tmp_path):
    con = connect(tmp_path / "t.db")
    n = sync_entries(con, segment(blocks)).added
    assert con.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == n
    first = con.execute(
        "SELECT MIN(date_start) FROM entries WHERE text LIKE '%Lena%kennengelernt%'"
    ).fetchone()[0]
    assert first == "1992-06-15"


def test_shift_return_line_break_after_date(tmp_path):
    from odf.opendocument import OpenDocumentText
    from odf.text import LineBreak, P

    doc = OpenDocumentText()
    for head, body in [("1995", "Ein schweres Jahr."), ("12.03.2021", "Heute   schön.")]:
        p = P(text=head)
        p.addElement(LineBreak())  # Shift+Return
        p.addText(body)
        doc.text.addElement(p)
        doc.text.addElement(P(text=""))  # empty paragraph between days
    doc.save(str(tmp_path / "lb.odt"))

    blocks = extract_blocks(tmp_path / "lb.odt")
    assert [b.text for b in blocks] == ["1995\nEin schweres Jahr.", "12.03.2021\nHeute schön."]
    entries = segment(blocks)
    assert [(e.precision, e.text) for e in entries] == [
        ("year", "Ein schweres Jahr."), ("day", "Heute schön.")]


def test_report_flags_typos_and_future_dates():
    def e(d, pos):
        return Entry(d, d, "day", d.strftime("%d.%m.%Y"), pos, "plain", ["x"])
    entries = [e(date(2013, 1, 10), 1), e(date(2023, 1, 18), 2), e(date(2013, 1, 19), 3),
               e(date(2013, 1, 25), 4), e(date(2030, 5, 1), 5)]
    r = build_report([], entries, today=date(2026, 10, 18))
    assert r.outliers == [(2, "10.01.2013", "18.01.2023", "19.01.2013")]
    assert r.future == [(5, "01.05.2030")]
    text = r.render()
    assert "Likely typos:       1" in text and "Future dates:       1" in text
