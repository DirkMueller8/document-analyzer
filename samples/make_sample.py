"""Generate samples/sample_entries.odt - a fully synthetic, fictional German training diary for dev and tests.

    backend/.venv/Scripts/python samples/make_sample.py
"""
from pathlib import Path

from odf.opendocument import OpenDocumentText
from odf.table import Table, TableCell, TableColumn, TableRow
from odf.text import P

PLAIN_BEFORE = [
    "Trainingstagebuch. Begonnen, um Umfang, Intensität und Fortschritt festzuhalten.",
    "Dez 1989 Erster Lauf im Schnee, 30 Minuten, locker. Ich bin zufrieden.",
    "Jan. 1990 Erstes Training im Schwimmverein mit Trainerin Petra, 60 Minuten, mittel. Großartig!",
    "1.3.1990 Erkältung, kein Training. Stimmung mies.",
    "Noch ein Absatz am selben Tag: Tee und leichte Dehnübungen helfen.",
    "15.6.92 Heute habe ich Lena beim Lauftreff kennengelernt. 10 km zusammen, locker.",
    "1995: Ein schweres Jahr, viel Arbeit, kaum Training.",
    "24.-26.12.1999 Skiurlaub, jeden Tag 4 Stunden auf der Piste, intensiv.",
]
TABLE_ROWS = [  # a period written as a table
    ("Datum", "Eintrag"),
    ("3.4.2005", "Radtour 40 Kilometer, 2 Stunden, mittel. Gewicht: 78,5 kg."),
    ("10.4.2005", "Erster Halbmarathon! 1:52 h, intensiv. Lampenfieber, aber es lief gut."),
    ("März 2006", "Knie verdreht beim Fußball, Arzt sagt Bänderdehnung. Zwei Wochen Pause."),
]
PLAIN_AFTER = [
    "12.01.2020 Krafttraining begonnen, 45 Minuten, leicht. Erste Einheit: Grundübungen.",
    "05.03.2021 Mit Lena gelaufen, 12 km, mittel. Gewicht: 76,2 kg, schlafe besser.",
    "December 2023 Started rowing, 30 minutes, moderate, first session went well.",
    "18.09.2024 Intervalltraining 6 x 800 m, intensiv. Puls bis 175.",
    "2025 - Rückblick: Laufen, Schwimmen und Radfahren geben mir Struktur.",
    "1989 war übrigens das Jahr, in dem alles anfing.",  # NOT a date (no separator)
]


def build(path: Path) -> None:
    doc = OpenDocumentText()
    for t in PLAIN_BEFORE:
        doc.text.addElement(P(text=t))
    table = Table(name="Training2005")
    table.addElement(TableColumn(numbercolumnsrepeated=2))
    for a, b in TABLE_ROWS:
        row = TableRow()
        for cell_text in (a, b):
            cell = TableCell()
            cell.addElement(P(text=cell_text))
            row.addElement(cell)
        table.addElement(row)
    doc.text.addElement(table)
    for t in PLAIN_AFTER:
        doc.text.addElement(P(text=t))
    doc.save(str(path))


if __name__ == "__main__":
    out = Path(__file__).with_name("sample_entries.odt")
    build(out)
    print(f"wrote {out}")
