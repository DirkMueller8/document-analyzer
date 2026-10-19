# document-analyzer: instructions for Claude Code

Local, agentic question answering over a personal diary kept as a password-protected `.odt`
(the public edition uses a training diary as its example). Everything runs on one Windows 11
desktop (RTX 4070 12 GB, 64 GB RAM): Ollama on 127.0.0.1, SQLite with FTS5 and sqlite-vec.
See `README.md`, `docs/ARCHITECTURE.md` and `docs/SECURITY.md` first.

## Non-negotiable rules
- **Never read, list, write or print anything from `data/` or any real `*.odt`.** `data/` is an encrypted
  vault mount. Only `samples/` (synthetic) may be opened. Work on the code with the synthetic sample.
- **Never print diary text, names, numbers from the real data, or the contents of the denylist.** Report
  counts and positions only. The user shares real output only if they choose to.
- **Only `scripts/publish.ps1 -Message "..."` publishes.** Never `git push` the main branch (the pre-push hook
  blocks it). Ask before publishing: it is visible outside the machine.
- **Commits go through the leak-guard hooks** (`scripts/hooks`). They need the user environment variable
  `LEAK_GUARD_DENYLIST`; without it they refuse. In a long-running shell export it for that command.
  If a hook blocks a commit, reword the text; never weaken the hook or the denylist.
- Public files (code, docs, samples, config examples, commit messages) must stay generic and
  training-themed. Do not add personal details, real names, paths to personal data, or the vault location.
- Keep `.claude/settings.json` valid JSON (an invalid file can disable every rule in it).

## Working
- Python 3.14 venv: `backend\.venv\Scripts\python`. Tests: `cd backend; .venv\Scripts\python -m pytest -q`
  (all must pass before a commit). New behavior needs a test with synthetic data.
- Shortcuts from the repo root: `.\ask.ps1`, `.\sync.ps1`, `.\weight.ps1`, `.\scripts\check-leftovers.ps1`,
  `.\scripts\vault-open.ps1` / `vault-close.ps1` (administrator).
- Layout: `backend/ingest` (decrypt, parse, dates, sync), `backend/search` (chunk, index, hybrid search, Q&A,
  Ollama client), `backend/analysis` (measurements, weight statistics), `backend/privacy` (gateway, cloud chart),
  `backend/tests`, `config` (example YAML), `scripts`, `docs`, `samples`.
- Design rules: numbers come from SQL or patterns, never from the language model; every answer cites entry
  dates; local models only (the Ollama client refuses non-loopback hosts and cloud models); the only egress is
  the privacy gateway (strict schema, user approval, logged).
- Next phase: per-entry extraction with a local model (see the roadmap in `README.md`).

## Tooling quirks on this machine
- The Bash tool loses backslashes inside heredocs and sed/python one-liners. For files containing
  backslashes (Windows paths, regexes, JSON escapes) use the Write and Edit tools, and use forward slashes
  in shell commands. Edit requires a prior Read of the file.
- Short denylist terms can match inside ordinary words. If a hook blocks a commit, find the offending
  word with a local search (without printing denylist terms) and reword it, for example "upcoming" for a
  synonym of the blocked word.
- Operator-specific notes, configuration and handoff documents are kept outside this repository. The
  project memory of the agent points to them.
- Reply in the language the user writes in (mostly German); keep code, comments and public docs in English.
