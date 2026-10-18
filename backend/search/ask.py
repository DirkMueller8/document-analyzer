"""Ask a question about the corpus - answered by the LOCAL model, with date citations.

    python -m search.ask "Wann bin ich meinen ersten Halbmarathon gelaufen?"
    python -m search.ask "Wie viel habe ich im Sommer 2004 trainiert?" --from 2004-06 --to 2004-08
    python -m search.ask "Knie" --search-only          # just show the matching excerpts
    python -m search.ask --first "Halbmarathon"        # earliest mentions of a term, no LLM

Everything runs on this machine (Ollama on 127.0.0.1). The output contains your own text.
"""
from __future__ import annotations

import argparse
import calendar
import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

from ingest.store import connect

from .hybrid import STOPWORDS, Hit, first_mentions, search
from .ollama_client import CHAT_MODEL, EMBED_MODEL, OllamaError, chat_stream, embed, require

DATA = Path(__file__).resolve().parents[2] / "data"

SYSTEM = """Du beantwortest Fragen zu einem persönlichen Tagebuch. Grundlage sind ausschließlich die \
Auszüge unten; jeder beginnt mit seinem Datum in eckigen Klammern.
Regeln:
- Belege jede Aussage mit dem Datum in eckigen Klammern, z. B. [12.03.2021].
- Wenn die Auszüge die Frage nicht beantworten, sag das klar, statt zu raten.
- Bei "zum ersten Mal"-Fragen: nenne den frühesten Auszug, der es tatsächlich belegt, und weise darauf \
hin, dass frühere, nicht erfasste Erwähnungen möglich sind.
- Antworte in der Sprache der Frage, knapp und sachlich. Keine Diagnosen, keine Ratschläge."""

FIRST_WORDS = re.compile(r"\b(erste[nrs]?|erstmals|zum ersten mal|first)\b", re.IGNORECASE)


def parse_when(value: str | None, end: bool = False) -> date | None:
    """'2004', '2004-06' or '2004-06-15' -> first (or last) day of that period."""
    if not value:
        return None
    parts = [int(p) for p in value.split("-")]
    if len(parts) == 1:
        return date(parts[0], 12, 31) if end else date(parts[0], 1, 1)
    if len(parts) == 2:
        y, m = parts
        return date(y, m, calendar.monthrange(y, m)[1] if end else 1)
    return date(*parts)


def names_in(question: str) -> list[str]:
    """Capitalised words that are not sentence starts or stopwords - likely names."""
    words = re.findall(r"(?<![.!?]\s)(?<!^)\b([A-ZÄÖÜ][\wäöüß-]+)", question)
    return [w for w in dict.fromkeys(words) if w.lower() not in STOPWORDS]


def gather(con, question: str, k: int, date_from, date_to) -> list[Hit]:
    hits = search(con, question, lambda q: embed([q])[0], k=k, date_from=date_from, date_to=date_to)
    if FIRST_WORDS.search(question):
        seen = {h.chunk_id for h in hits}
        for name in names_in(question):
            hits += [h for h in first_mentions(con, name, n=3) if h.chunk_id not in seen]
    return sorted(hits, key=lambda h: (h.date_start or "0000", h.chunk_id))


class Clock:
    """Timestamps on stderr: wall-clock time plus seconds since the start of the question."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.start = time.perf_counter()
        if enabled:
            print(f"[{datetime.now():%H:%M:%S}]   +0.0 s  Frage gestartet", file=sys.stderr, flush=True)

    def mark(self, label: str) -> None:
        if self.enabled:
            print(f"[{datetime.now():%H:%M:%S}] {time.perf_counter() - self.start:+6.1f} s  {label}",
                  file=sys.stderr, flush=True)

    def summary(self, stats: dict) -> None:
        """Ollama's own measurements: model load, reading the prompt, generating (incl. thinking)."""
        if not (self.enabled and stats.get("eval_duration")):
            return
        s = lambda key: (stats.get(key) or 0) / 1e9  # noqa: E731 - nanoseconds -> seconds
        gen = stats.get("eval_count") or 0
        print(f"           Modell laden {s('load_duration'):.1f} s · Auszüge lesen "
              f"{stats.get('prompt_eval_count') or 0} Tokens in {s('prompt_eval_duration'):.1f} s · "
              f"erzeugt {gen} Tokens in {s('eval_duration'):.1f} s ({gen / s('eval_duration'):.1f} Tokens/s)",
              file=sys.stderr, flush=True)


def context(hits: list[Hit]) -> str:
    return "\n\n".join(f"[{h.raw_date or 'ohne Datum'}] {h.text}" for h in hits)


def show(hits: list[Hit], width: int = 220) -> None:
    for h in hits:
        text = " ".join(h.text.split())
        print(f"[{h.raw_date or '-'}] ({'+'.join(h.sources)}) {text[:width]}{'…' if len(text) > width else ''}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("question", nargs="?")
    ap.add_argument("--db", type=Path, default=DATA / "corpus.db")
    ap.add_argument("--k", type=int, default=12, help="number of excerpts given to the model")
    ap.add_argument("--from", dest="date_from", help="YYYY, YYYY-MM or YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to")
    ap.add_argument("--search-only", action="store_true")
    ap.add_argument("--first", metavar="TERM", help="earliest mentions of TERM (no LLM)")
    ap.add_argument("--model", default=CHAT_MODEL)
    ap.add_argument("--show-thinking", action="store_true", help="also print the model's reasoning")
    ap.add_argument("--no-times", action="store_true", help="don't print timestamps")
    args = ap.parse_args(argv)
    if not args.db.exists():
        raise SystemExit(f"{args.db} not found. Open the vault and run the import first.")
    con = connect(args.db)

    try:
        if args.first:
            show(first_mentions(con, args.first, n=10))
            return
        if not args.question:
            ap.error("a question (or --first TERM) is required")
        clock = Clock(enabled=not args.no_times)
        require(EMBED_MODEL)
        hits = gather(con, args.question, args.k, parse_when(args.date_from), parse_when(args.date_to, end=True))
        clock.mark(f"Suche fertig ({len(hits)} Auszüge)")
        if not hits:
            print("Nothing found. Try other words or a wider date range.")
            return
        if args.search_only:
            show(hits)
            return
        require(args.model)
        messages = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": f"Auszüge:\n\n{context(hits)}\n\nFrage: {args.question}"}]
        clock.mark(f"Sprachmodell aufgerufen ({args.model})")
        thinking_shown = answer_started = False
        stats: dict = {}
        for kind, piece in chat_stream(messages, model=args.model):
            if kind == "stats":
                stats = json.loads(piece)
                continue
            if kind == "thinking":
                if not thinking_shown:
                    clock.mark("denkt nach …")
                    thinking_shown = True
                if args.show_thinking:
                    print(piece, end="", flush=True, file=sys.stderr)
                continue
            if not answer_started:
                if args.show_thinking:
                    print("\n", file=sys.stderr)
                clock.mark("Antwort beginnt")
                piece = piece.lstrip()
                answer_started = True
            print(piece, end="", flush=True)
        print(file=sys.stderr)
        clock.mark("Antwort fertig")
        clock.summary(stats)
        print(f"— {len(hits)} Auszüge, Modell {args.model} (lokal)", file=sys.stderr)
    except OllamaError as e:
        raise SystemExit(str(e))


if __name__ == "__main__":
    main()
