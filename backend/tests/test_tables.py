from datetime import date

from odf.opendocument import OpenDocumentText
from odf.table import Table, TableCell, TableColumn, TableRow
from odf.text import P

from ingest.dates import parse_leading_date
from ingest.inspect_tables import inspect, shape
from ingest.odt_parser import extract_blocks
from ingest.segment import segment


def build(tmp_path, rows, columns=1):
    """rows: list of rows; a row is a list of cells; a cell is a list of paragraphs
    or a (paragraphs, date_value) tuple for a date-formatted cell."""
    doc = OpenDocumentText()
    doc.text.addElement(P(text="31.12.2020 Davor normal."))
    t = Table(name="T")
    t.addElement(TableColumn(numbercolumnsrepeated=columns))
    for row in rows:
        tr = TableRow()
        for cell in row:
            paras, dv = cell if isinstance(cell, tuple) else (cell, None)
            tc = TableCell(valuetype="date", datevalue=dv) if dv else TableCell()
            for text in paras:
                tc.addElement(P(text=text))
            tr.addElement(tc)
        t.addElement(tr)
    doc.text.addElement(t)
    doc.text.addElement(P(text="01.01.2022 Danach normal."))
    path = tmp_path / "t.odt"
    doc.save(str(path))
    return extract_blocks(path)


def dated(entries):
    return [(e.date_start, e.text) for e in entries if e.date_start]


def test_one_column_row_per_day_with_paragraphs(tmp_path):
    blocks = build(tmp_path, [
        [["04.01.2021", "Erster Absatz.", "Zweiter Absatz."]],
        [["Di, 05.01.2021", "Text vom Dienstag."]],
    ])
    assert dated(segment(blocks)) == [
        (date(2020, 12, 31), "Davor normal."),
        (date(2021, 1, 4), "Erster Absatz.\nZweiter Absatz."),
        (date(2021, 1, 5), "Text vom Dienstag."),
        (date(2022, 1, 1), "Danach normal."),
    ]


def test_one_column_many_days_in_one_cell(tmp_path):
    blocks = build(tmp_path, [[["04.01.2021 Montag-Text.", "05.01.2021", "Dienstag-Text.", "06.01.2021 Mittwoch."]]])
    assert [d for d, _ in dated(segment(blocks))] == [
        date(2020, 12, 31), date(2021, 1, 4), date(2021, 1, 5), date(2021, 1, 6), date(2022, 1, 1)]


def test_date_in_second_column(tmp_path):
    blocks = build(tmp_path, [[["Notiz"], ["04.01.2021"], ["Text."]]], columns=3)
    assert dated(segment(blocks))[1] == (date(2021, 1, 4), "Notiz\tText.")


def test_date_formatted_cell_uses_stored_value(tmp_path):
    blocks = build(tmp_path, [[(["Montag"], "2021-01-04"), ["Text."]]], columns=2)
    assert dated(segment(blocks))[1] == (date(2021, 1, 4), "Text.")


def test_weekday_prefix_and_written_out_dates():
    assert parse_leading_date("Montag, 4. Januar 2021 Text").start == date(2021, 1, 4)
    assert parse_leading_date("Mo. 04.01.2021").start == date(2021, 1, 4)
    assert parse_leading_date("4 Jan 2021: x").start == date(2021, 1, 4)
    assert parse_leading_date("Sonntag war schön") is None
    assert parse_leading_date("So 1995 war") is None


def test_inspector_shows_structure_not_content(tmp_path):
    blocks = build(tmp_path, [[["04.01.2021", "Geheimer Text."]], [["Notiz Lena"]]])
    report = inspect(blocks)
    assert "Table 1:" in report and "dated: 1" in report
    assert "Geheim" not in report and "Lena" not in report and "Notiz" not in report
    assert shape("Mo, 04.01.2021") == "<W>, 99.99.9999"
    assert shape("x" * 30) == "<text 30 chars>"
