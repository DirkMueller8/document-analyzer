# Security & threat model

This repository is public by design. **Knowing the code must not help an attacker.** Following Kerckhoffs's principle, all protection rests on secrets that never appear here: the document password, the vault password and the machine itself. The repo contains no personal data, no paths to it and no hints about its content.

## Assets
1. The training diary (password-protected `.odt`). A diary like that is health data: injuries, illnesses, body weight, daily routines and locations.
2. Derived data: the database with full text, embeddings, extracted training, injury and weight data, logs, overrides
3. Text in transit: prompts and answers between UI, backend and LLM

## Threats and controls
| Threat | Control |
|---|---|
| **Someone reads the public repo** and learns the architecture | Nothing to gain: no data, no personal paths, synthetic fictional samples. A leak-guard hook blocks data files and private terms on every commit and push. |
| **Brute-forcing a stolen source file** | LibreOffice ODF 1.4 encryption with AES-256-GCM and an Argon2id key derivation (memory-hard). With a long passphrase (6+ random words) brute force is infeasible. |
| **Stolen laptop or disk** | Derived data sits in a BitLocker XTS-AES-256 VHDX that is locked when not in use. Full-disk BitLocker on the system drive also covers swap, hibernation and temp files. |
| **Data leaving via the network** | The LLM (Ollama), backend and UI bind to `127.0.0.1`. The Ollama client in this code refuses non-loopback hosts and cloud-hosted model names. Windows Firewall outbound rules block the Python runtime and Ollama. Offline flags (`HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE`) after model download. No libraries with telemetry. |
| **The optional cloud analyst** | Only path out. Strict schema (numbers, dates, fixed labels, no free text), pseudonymized identifiers, a per-request payload preview with user approval, and a local egress log. Off by default. |
| **Plaintext written to disk during processing** | Decryption happens in memory. The validation report contains no text unless `--show-snippets` is passed. All writes go into the vault, and the import refuses to write while the vault is closed. |
| **AI coding assistants reading private data** | `.claude/settings.json` denies access to `data/` and `*.odt`. Development uses synthetic samples only. |
| **Supply chain**: a malicious dependency or pushed code | The firewall blocks outbound traffic from the runtime, so exfiltration fails. Pin and review dependency updates. Enable 2FA on the GitHub account and protect the main branch. Never run code from pull requests while the vault is open. |
| **Malware in the logged-in session** | Biggest residual risk: anything running as you while the vault is unlocked can read it. Keep the OS and browser patched, avoid untrusted software, and close the vault after use (`scripts\vault-close.ps1`). |

## What is (and isn't) in the repo
| In the repo | Never in the repo |
|---|---|
| Code, schema, docs, scripts | The source document, `data/` (vault mount point), any `.db` / `.vhdx` |
| Synthetic, fictional sample documents | Real names, places or content |
| Test password `Synthetic-Test-Key-42` for the **synthetic** fixtures only | Real passwords or keys (the source password is asked for on each run and never stored) |
| Example taxonomy with generic domains | The personal taxonomy (lives in the vault) |

## Hardening checklist
- [ ] Long, unique passphrases for the document and the vault, stored in a password manager
- [ ] BitLocker on the system drive (TPM + PIN)
- [ ] Vault file outside cloud-synced folders. Set `VAULT_PATH` to choose its location.
- [ ] Firewall outbound block rules for `ollama.exe` and the backend's `python.exe`
- [ ] `git config core.hooksPath scripts/hooks`, and keep the local denylist up to date
- [ ] 2FA on GitHub, branch protection on `main`
- [ ] Encrypted backups of the vault file, and a copy of `overrides.yaml` and the taxonomy
- [ ] Close the vault when done

## Reporting
Please report security issues privately through GitHub's "Report a vulnerability" feature rather than in public issues.
