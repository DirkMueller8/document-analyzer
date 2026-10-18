"""Split an entry into retrieval chunks (~400-500 tokens), never across entry boundaries."""
from __future__ import annotations

import re

TARGET_CHARS = 1500   # roughly 400-500 tokens of German text
OVERLAP_CHARS = 200   # tail of the previous chunk repeated at the start of the next one

_SENTENCE = re.compile(r"(?<=[.!?…])\s+")


def _pieces(text: str, target: int) -> list[str]:
    """Paragraphs; overly long ones are split at sentence ends (hard split as last resort)."""
    out: list[str] = []
    for para in (p.strip() for p in text.split("\n")):
        if not para:
            continue
        if len(para) <= target:
            out.append(para)
            continue
        buf = ""
        for sentence in _SENTENCE.split(para):
            while len(sentence) > target:
                out.append(sentence[:target])
                sentence = sentence[target:]
            if buf and len(buf) + 1 + len(sentence) > target:
                out.append(buf)
                buf = sentence
            else:
                buf = f"{buf} {sentence}".strip()
        if buf:
            out.append(buf)
    return out


def _tail(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    cut = text[-n:]
    space = cut.find(" ")
    return "…" + (cut[space + 1:] if 0 <= space < n // 2 else cut)


def chunk_text(text: str, target: int = TARGET_CHARS, overlap: int = OVERLAP_CHARS) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    for piece in _pieces(text, target):
        if current and len("\n".join(current)) + 1 + len(piece) > target:
            chunks.append("\n".join(current))
            current = [_tail(chunks[-1], overlap)] if overlap else []
        current.append(piece)
    if current:
        chunks.append("\n".join(current))
    return chunks
