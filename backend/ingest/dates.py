"""Detect the date at the start of a dated-entry paragraph.

Supported (German and English):
    12.03.2021      1.1.1999      1.1.99        24.-26.12.1999
    Dez 1989        Dez. 1989     Dezember 1989  December 1989   März 2001
    1989:           1989 -        1989<line break>  (year only: needs a separator, so "1989 war ..." is no date)
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

MONTHS: dict[str, int] = {
    # German
    "januar": 1, "jan": 1, "jänner": 1, "jän": 1,
    "februar": 2, "feb": 2, "febr": 2,
    "märz": 3, "maerz": 3, "mär": 3, "mrz": 3,
    "april": 4, "apr": 4,
    "mai": 5,
    "juni": 6, "jun": 6,
    "juli": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "okt": 10,
    "november": 11, "nov": 11,
    "dezember": 12, "dez": 12,
    # English (only those that differ)
    "january": 1, "february": 2, "march": 3, "mar": 3, "may": 5,
    "june": 6, "july": 7, "october": 10, "oct": 10, "december": 12, "dec": 12,
}

_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))

WEEKDAYS = (
    "montag dienstag mittwoch donnerstag freitag samstag sonnabend sonntag "
    "monday tuesday wednesday thursday friday saturday sunday "
    "mon tue tues wed thu thur thurs fri sat sun mo di mi do fr sa so"
).split()
_WEEKDAY_ALT = "|".join(sorted(WEEKDAYS, key=len, reverse=True))

# Leading whitespace / bullets and an optional weekday ("Mo, ", "Montag ") are tolerated.
_LEAD = rf"^[\s\-–•*]*(?:(?:{_WEEKDAY_ALT})\.?,?\s+)?"
_TAIL = r"(?=$|[\s:,;.\-–)])"
_I = re.IGNORECASE

RE_DAY_RANGE = re.compile(_LEAD + r"(\d{1,2})\.\s*[-–]\s*(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})" + _TAIL, _I)
# a 2-digit year must follow the dot directly ("1.1.99"); otherwise "12.03. 85,5 kg" would be 1985
RE_DAY = re.compile(_LEAD + r"(\d{1,2})\.\s?(\d{1,2})\.(\s?\d{4}|\d{2})" + _TAIL, _I)
RE_DAY_NAMED = re.compile(_LEAD + rf"(\d{{1,2}})\.?\s+({_MONTH_ALT})\.?\s+(\d{{4}})" + _TAIL, _I)  # 4. Januar 2021
RE_MONTH = re.compile(_LEAD + rf"({_MONTH_ALT})\.?\s+(\d{{4}})" + _TAIL, _I)
RE_YEAR = re.compile(_LEAD + r"((?:19|20)\d{2})[ \t]*(?:[:\-–]|\n|$)", _I)


@dataclass(frozen=True)
class DateInfo:
    start: date
    end: date
    precision: str  # "day" | "month" | "year"
    raw: str        # the matched date text, stripped
    rest: str       # paragraph text after the date


def _year(y: str) -> int:
    n = int(y)
    if len(y) == 2:
        return 1900 + n if n >= 30 else 2000 + n
    return n


def _plausible(y: int) -> bool:
    return 1940 <= y <= date.today().year + 1


def _rest(text: str, end: int) -> str:
    rest = text[end:].lstrip(" \t\n:,;.-–").strip()
    # "19.10.2026 Montag" with nothing else is a date line, not content
    return "" if rest.strip(" ,.;:-–()").lower() in WEEKDAYS else rest


def parse_leading_date(text: str) -> DateInfo | None:
    """Return the date the paragraph starts with, or None."""
    if m := RE_DAY_RANGE.match(text):
        d1, d2, mo, y = int(m[1]), int(m[2]), int(m[3]), _year(m[4])
        try:
            start, end = date(y, mo, d1), date(y, mo, d2)
        except ValueError:
            start = None
        if start and start <= end and _plausible(y):
            return DateInfo(start, end, "day", m.group(0).strip(" -–•*\t"), _rest(text, m.end()))

    if m := RE_DAY.match(text):
        d, mo, y = int(m[1]), int(m[2]), _year(m[3].strip())
        try:
            dt = date(y, mo, d)
        except ValueError:
            dt = None
        if dt and _plausible(y):
            return DateInfo(dt, dt, "day", m.group(0).strip(" -–•*\t"), _rest(text, m.end()))

    if m := RE_DAY_NAMED.match(text):
        d, mo, y = int(m[1]), MONTHS[m[2].lower()], int(m[3])
        try:
            dt = date(y, mo, d)
        except ValueError:
            dt = None
        if dt and _plausible(y):
            return DateInfo(dt, dt, "day", m.group(0).strip(" -–•*\t"), _rest(text, m.end()))

    if m := RE_MONTH.match(text):
        mo, y = MONTHS[m[1].lower()], int(m[2])
        if _plausible(y):
            last = calendar.monthrange(y, mo)[1]
            return DateInfo(date(y, mo, 1), date(y, mo, last), "month",
                            m.group(0).strip(" -–•*\t"), _rest(text, m.end()))

    if m := RE_YEAR.match(text):
        y = int(m[1])
        if _plausible(y):
            return DateInfo(date(y, 1, 1), date(y, 12, 31), "year", m[1], _rest(text, m.end()))

    return None
