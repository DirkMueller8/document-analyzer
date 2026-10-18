"""Extract text blocks from an .odt file in document order.

Paragraphs, headings and list items become one block each. Every table row becomes
one block with its cells joined by a tab, so a "date | text" table period reads
like normal dated paragraphs. Cells are also kept separately (with any date value
LibreOffice stored for date-formatted cells), so a date in another column is found too.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from odf import teletype
from odf.namespaces import OFFICENS, TABLENS, TEXTNS
from odf.opendocument import load

PARAGRAPHS = ((TEXTNS, "p"), (TEXTNS, "h"))


@dataclass(frozen=True)
class Block:
    pos: int                          # running index in the document, used as source position
    text: str
    kind: str                         # "plain" | "table"
    cells: tuple[str, ...] = ()       # table rows: text per cell
    date_values: tuple[str, ...] = ()  # table rows: office:date-value per cell ("" if none)
    table: int | None = None          # table rows: running table number


def _text(el) -> str:
    """Normalize whitespace but keep line breaks (Shift+Return), e.g. "1995\\nText"."""
    lines = (" ".join(line.split()) for line in teletype.extractText(el).split("\n"))
    return "\n".join(line for line in lines if line)


def _paragraphs(el) -> list[str]:
    """Text of every paragraph below el, one item each (odfpy would glue them together)."""
    out: list[str] = []
    for child in el.childNodes:
        qname = getattr(child, "qname", None)
        if qname in PARAGRAPHS:
            if t := _text(child):
                out.append(t)
        elif qname is not None:
            out.extend(_paragraphs(child))
    return out


class _Walker:
    def __init__(self) -> None:
        self.out: list[tuple] = []
        self.tables = 0

    def walk(self, el, table: int | None = None) -> None:
        for child in el.childNodes:
            qname = getattr(child, "qname", None)
            if qname is None:
                continue
            if qname in PARAGRAPHS:
                self.out.append((_text(child), "plain", (), (), None))
            elif qname == (TABLENS, "table"):
                self.tables += 1
                self.walk(child, self.tables)
            elif qname == (TABLENS, "table-row"):
                cells = [c for c in child.childNodes if getattr(c, "qname", None) == (TABLENS, "table-cell")]
                paras = [_paragraphs(c) for c in cells]
                dvals = tuple(c.getAttrNS(OFFICENS, "date-value") or "" for c in cells)
                filled = [i for i, p in enumerate(paras) if p]
                if len(filled) == 1:
                    # one-column layout: read the cell paragraph by paragraph, like normal text
                    i = filled[0]
                    for n, p in enumerate(paras[i]):
                        self.out.append((p, "table", (p,), (dvals[i] if n == 0 else "",), table))
                else:
                    texts = tuple("\n".join(p) for p in paras)
                    self.out.append(("\t".join(t for t in texts if t), "table", texts, dvals, table))
            else:
                # lists, sections, header-rows, ... -> descend
                self.walk(child, table)


def extract_blocks(source: str | Path | BinaryIO) -> list[Block]:
    """Read blocks from an .odt path or an in-memory file (e.g. from odf_crypto)."""
    doc = load(source if hasattr(source, "read") else str(source))
    w = _Walker()
    w.walk(doc.text)
    return [Block(i, *row) for i, row in enumerate(w.out) if row[0].strip()]
