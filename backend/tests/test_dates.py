from datetime import date

import pytest

from ingest.dates import parse_leading_date


@pytest.mark.parametrize(
    "text, start, end, precision, rest",
    [
        ("12.03.2021 Heute schön", date(2021, 3, 12), date(2021, 3, 12), "day", "Heute schön"),
        ("1.1.1999 Neujahr", date(1999, 1, 1), date(1999, 1, 1), "day", "Neujahr"),
        ("1.1.99: Neujahr", date(1999, 1, 1), date(1999, 1, 1), "day", "Neujahr"),
        ("3.4.05 Joggen", date(2005, 4, 3), date(2005, 4, 3), "day", "Joggen"),
        ("24.-26.12.1999 Allgäu", date(1999, 12, 24), date(1999, 12, 26), "day", "Allgäu"),
        ("Dez 1989 Weihnachten", date(1989, 12, 1), date(1989, 12, 31), "month", "Weihnachten"),
        ("Dez. 1989 Weihnachten", date(1989, 12, 1), date(1989, 12, 31), "month", "Weihnachten"),
        ("Dezember 1989", date(1989, 12, 1), date(1989, 12, 31), "month", ""),
        ("März 2006 Rücken", date(2006, 3, 1), date(2006, 3, 31), "month", "Rücken"),
        ("Februar 2000 x", date(2000, 2, 1), date(2000, 2, 29), "month", "x"),
        ("December 2023 AI", date(2023, 12, 1), date(2023, 12, 31), "month", "AI"),
        ("1995: Schweres Jahr", date(1995, 1, 1), date(1995, 12, 31), "year", "Schweres Jahr"),
        ("2025 - Rückblick", date(2025, 1, 1), date(2025, 12, 31), "year", "Rückblick"),
        ("1995\nEin schweres Jahr", date(1995, 1, 1), date(1995, 12, 31), "year", "Ein schweres Jahr"),
        ("12.03.2021\nHeute schön", date(2021, 3, 12), date(2021, 3, 12), "day", "Heute schön"),
        ("Dez 1989\nWeihnachten", date(1989, 12, 1), date(1989, 12, 31), "month", "Weihnachten"),
        ("01.07. 2004 Leerzeichen", date(2004, 7, 1), date(2004, 7, 1), "day", "Leerzeichen"),
        ("  - 05.03.2021 eingerückt", date(2021, 3, 5), date(2021, 3, 5), "day", "eingerückt"),
    ],
)
def test_parses(text, start, end, precision, rest):
    d = parse_leading_date(text)
    assert d is not None
    assert (d.start, d.end, d.precision, d.rest) == (start, end, precision, rest)


@pytest.mark.parametrize(
    "text",
    [
        "1989 war das Jahr, in dem alles anfing.",  # year without separator
        "Heute am 12.03.2021 war ich müde.",        # date not at the start
        "31.02.2020 ungültig",                      # invalid day
        "12.13.2020 ungültig",                      # invalid month
        "3.5 Stunden geflogen",                     # not a date
        "Mai war schön",                            # month without year
        "1.1.1850 zu alt",                          # implausible year
        "12.03. 85,5 kg",                           # day.month without year + weight
        "12.03 85,5 kg",
        "12.03.: 84 kg",
        "12.03.",
        "",
    ],
)
def test_rejects(text):
    assert parse_leading_date(text) is None


def test_weekday_after_date_is_not_content():
    assert parse_leading_date("19.10.2026 Montag").rest == ""
    assert parse_leading_date("19.10.2026 (Mo)").rest == ""
    assert parse_leading_date("19.10.2026 Montag war schön").rest == "Montag war schön"
