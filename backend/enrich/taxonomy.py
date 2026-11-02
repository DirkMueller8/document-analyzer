"""Load the activity domains (data/taxonomy.yaml in the vault, else the example in config/)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIGS = [ROOT / "data" / "taxonomy.yaml", ROOT / "config" / "taxonomy.example.yaml"]


@dataclass(frozen=True)
class Domain:
    name: str
    description: str
    hints: tuple[str, ...]


def load_domains(path: str | Path | None = None) -> list[Domain]:
    candidates = [Path(path)] if path else DEFAULT_CONFIGS
    cfg_path = next((p for p in candidates if p.exists()), None)
    if cfg_path is None:
        raise FileNotFoundError("No taxonomy found (data/taxonomy.yaml or config/taxonomy.example.yaml).")
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return [Domain(name, (spec or {}).get("description", ""), tuple(str(h) for h in (spec or {}).get("hints") or []))
            for name, spec in (cfg.get("domains") or {}).items()]


def fingerprint(domains: list[Domain]) -> str:
    """Changes whenever the taxonomy changes, so results made with an older taxonomy are redone."""
    raw = json.dumps([[d.name, d.description, list(d.hints)] for d in domains], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
