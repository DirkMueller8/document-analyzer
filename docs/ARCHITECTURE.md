# Architecture

## Goals
1. Answer factual and trend questions about a personal training diary kept as free text over many years (thousands of entries, multilingual): what was trained, how long, how hard, injuries, body weight.
2. Run on a single desktop PC. No text leaves the machine.
3. Stay usable with daily additions, so updates must be incremental.

## Components
| Layer | Technology | Notes |
|---|---|---|
| UI *(upcoming)* | React + TypeScript (Vite), ECharts | Streaming chat, citation chips that open the source entry, interactive charts |
| API *(upcoming)* | FastAPI on `127.0.0.1` | REST + server-sent events |
| Agents *(upcoming)* | Typed tool-calling agents on the local Ollama server | Narrow toolsets per agent |
| Inference | Ollama (CUDA), loopback only | `qwen3:14b` interactive, `qwen3:30b-a3b` for harder or batch work, `bge-m3` embeddings |
| Retrieval | SQLite FTS5 + sqlite-vec | Hybrid search with reciprocal rank fusion (RRF) |
| Storage | One SQLite file inside a BitLocker VHDX | Single file, no telemetry, easy to back up encrypted |
| Cloud (optional) | Anthropic API behind a privacy gateway | Gets only gateway-approved, anonymized monthly aggregates |

## Ingestion pipeline (`backend/ingest/`)
| Step | Module | What it does |
|---|---|---|
| Decrypt | `odf_crypto.py` | Opens LibreOffice password-protected files **in memory**. ODF 1.4 whole-package encryption (AES-256-GCM, key from Argon2id over SHA-256 of the password) and legacy ODF 1.2 per-file encryption (AES-256-CBC, PBKDF2, sha256-1k checksum). Wrong passwords are detected via the GCM tag or the checksum. |
| Parse | `odt_parser.py` | Walks paragraphs, headings, lists and **table rows** in document order. Multi-column rows: the date may sit in any of the first three columns or in a date-formatted cell. One-column tables are read paragraph by paragraph. Line breaks (Shift+Return) are kept. |
| Date | `dates.py` | Leading-date detection: `dd.mm.yyyy`, `d.m.yy`, day ranges `24.-26.12.1999`, weekday prefixes (`Mo, 04.01.2021`), written-out dates (`4. Januar 2021`), German/English month names (`Dez 1989`), and year-only headings that require a separator. A two-digit year must follow the dot directly, so `12.03. 85,5 kg` isn't a date. Each date stores a `[start, end]` range and a precision (day/month/year). |
| Segment | `segment.py` | A dated paragraph starts an entry, and undated paragraphs continue it. The validation report (per-year counts, undated entries, dates going backwards, likely date typos, future dates) contains **no text** unless asked. Manual fixes go in `overrides.yaml`. |
| Sync | `store.py` | Matches entries by SHA-256 of their text. Unchanged entries keep their row id, and with it their chunks, vectors and extracted data. Edited or removed entries are deleted, and their derived rows go with them (`ON DELETE CASCADE` + triggers). Empty date lines and pre-written future dates are skipped. |
| Measure | `analysis/measurements.py` | Numeric values (e.g. body weight) extracted **deterministically** with your own regex patterns (`measurements.yaml`). A date written in front of a value on the same line (`12.03.`, `Ref_12.03.2024`) overrides the entry date. |
| Chunk & embed | `search/chunk.py`, `search/index.py` | ~1500-character chunks on paragraph/sentence boundaries with overlap, never across entries. The date goes into the embedded text. `bge-m3` vectors into sqlite-vec, plus an FTS5 index. Incremental. |
| Extract *(phase 3)* | — | Per-entry structured extraction with JSON-schema output from a local LLM: activities with duration and intensity per configurable domain (`taxonomy.yaml`), injuries and recovery, training partners, milestones. Resumable and keyed by text hash. |

## Data model (`backend/db/schema.sql`)
- `entries`: date range, precision, raw date, text, hash, source position, plain/table
- `chunks` + `chunks_fts` + `chunks_vec`: retrieval units
- `measurements`: date, kind (e.g. weight), value, unit, source (entry date or a written reference date)
- `activities`: domain (from `taxonomy.yaml`), activity, duration in minutes, intensity, sentiment, note
- `milestones`: first / achievement / setback / decision (first half marathon, comeback after injury, …)
- `health_events`: injuries and illnesses with type (symptom / diagnosis / treatment / recovery) and severity
- `people`, `mentions`: an alias-merged index of training partners and coaches
- `entry_meta`: how the session felt (−5…+5), energy, sleep, themes

## Agents
| Agent | Tools | Typical questions |
|---|---|---|
| Router & synthesizer | dispatch, merge, cite | Splits compound questions and writes the final answer with date citations |
| Fact finder | `search_entries`, `get_entry`, `first_mentions` | "When did I run my first half marathon?", "What did I train in summer 2004?" |
| Training load & trends | `activity_series`, `correlate`, `make_chart` | "How did my weekly running volume develop between 2015 and 2025?" |
| Injuries & recovery | `health_events`, `search_entries` | "How long did the knee injury keep me off the bike?" |
| Body weight | `weight_stats`, `make_chart` | "How did my weight follow my training volume?" |

Design rules:
- Specialists get **narrow toolsets**, which local models handle more reliably.
- Numbers come from SQL, never from the LLM's memory. The LLM explains them.
- Charts are returned as **JSON chart specs** and drawn locally.
- Correlations are labeled as such, never presented as causation.
- A golden-question benchmark (questions with known answers) guards every model or prompt change.

## Local question answering today (`search/ask.py`)
1. Hybrid retrieval: FTS5 (safe OR-query of the meaningful words) and vector KNN, merged with RRF, optional date filter. "First time" questions also pull the chronologically earliest mentions of the names in the question.
2. The excerpts go to the local model **with their dates**, and the system prompt requires a date citation for every statement and an explicit "not in the excerpts" instead of guessing.
3. qwen3 is called with thinking enabled. The reasoning arrives as a separate stream (with thinking disabled it leaks into the answer), and only the answer is printed. Timestamps and Ollama's token rates are shown on stderr.
4. The Ollama client refuses non-loopback hosts and cloud-hosted model names.

## Fitting the models to the hardware
- `qwen3:14b` runs about 85% on the 12 GB GPU: about 25 tokens/s, roughly 20 seconds for a cited answer including reasoning.
- `qwen3:30b-a3b` (mixture of experts) activates only about 3B parameters per token, so half its weights can sit in the 64 GB of system RAM. It's slower but more thorough.
- `bge-m3` embeds about 8,000 chunks in under three minutes.

## Optional cloud analyst: proof of concept (`privacy/`)
1. `gateway.py` builds a `WeightPayload`: monthly mean/min/max/days only, strict schema (unknown fields, text or day-level dates fail validation). Likely typos are excluded before anything leaves.
2. The exact payload is shown, and the user must type `ja` to approve.
3. A single Claude API call (`claude-opus-5-5`, structured JSON output, server-side refusal fallback) returns a **chart plan**: title, phases, annotations, insights. That's data, not code.
4. The plan is schema-validated, and the chart is rendered **locally** with matplotlib. Payload and response metadata are appended to `data/egress.log` in the vault.

## Testing
- Unit tests for every date format, table layouts, continuation paragraphs, overrides, incremental sync and report checks
- Decryption tests against **real LibreOffice output** in both encryption formats (`samples/tools/lo_encrypt.py`), with a fully synthetic training diary
- Search tests with a deterministic fake embedder: incremental indexing, deletion triggers, hybrid ranking, date filters, Ollama guards
- Gateway tests: text, unknown fields, day-level dates and strings are rejected
- Next: golden-question benchmark, leak test with the network adapter disabled
