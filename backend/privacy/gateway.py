"""Privacy gateway: the only code path that may send data to a cloud service.

Rules enforced here:
- Payloads are strict Pydantic schemas: numbers, ISO months and fixed labels. No free-text fields,
  no unknown fields, no day-level dates. Anything else fails validation and is never sent.
- The user sees the exact JSON that would leave the machine and must type "ja" to approve.
- Every approved request (payload + response) is appended to data/egress.log inside the vault.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from analysis.weight import load_series, outliers

DATA = Path(__file__).resolve().parents[2] / "data"
EGRESS_LOG = DATA / "egress.log"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class MonthlyStat(_Strict):
    month: str = Field(pattern=r"^(19|20)\d{2}-(0[1-9]|1[0-2])$")
    mean: float = Field(ge=20, le=300)
    min: float = Field(ge=20, le=300)
    max: float = Field(ge=20, le=300)
    days: int = Field(ge=1, le=31)


class WeightPayload(_Strict):
    """Monthly body-weight aggregates. Deliberately contains no names, no text and no exact days."""
    kind: Literal["body_weight_monthly_kg"] = "body_weight_monthly_kg"
    months: tuple[MonthlyStat, ...] = Field(min_length=1)


def build_weight_payload(con: sqlite3.Connection) -> WeightPayload:
    series = load_series(con)
    bad = {id(p) for p, _ in outliers(series)}  # likely typos never leave the machine
    by_month: dict[str, list[float]] = defaultdict(list)
    for p in series:
        if id(p) not in bad:
            by_month[p.day.strftime("%Y-%m")].append(p.value)
    return WeightPayload(months=tuple(
        MonthlyStat(month=m, mean=round(st.fmean(v), 1), min=round(min(v), 1), max=round(max(v), 1), days=len(v))
        for m, v in sorted(by_month.items())
    ))


def payload_json(payload: BaseModel) -> str:
    return payload.model_dump_json()


def preview(payload: BaseModel, destination: str) -> str:
    raw = payload_json(payload)
    data = json.loads(raw)
    rows = data.get("months", [])
    head = "\n".join(f"    {json.dumps(r)}" for r in rows[:5])
    tail = "\n".join(f"    {json.dumps(r)}" for r in rows[-3:]) if len(rows) > 8 else ""
    return "\n".join([
        "=" * 72,
        f"PRIVACY GATEWAY - data that would be sent to: {destination}",
        f"  type:   {data.get('kind')}",
        f"  rows:   {len(rows)} months ({rows[0]['month']} .. {rows[-1]['month']})" if rows else "  rows: 0",
        f"  size:   {len(raw.encode())} bytes, sha256 {hashlib.sha256(raw.encode()).hexdigest()[:16]}",
        "  fields: month (YYYY-MM), mean, min, max, days - no text, no names, no exact dates",
        "  first rows:",
        head,
        *(["    ...", tail] if tail else []),
        "=" * 72,
    ])


def confirm(prompt: str = 'Send exactly this? Type "ja" to approve: ') -> bool:
    try:
        return input(prompt).strip().lower() == "ja"
    except EOFError:
        return False


def log_egress(destination: str, payload: BaseModel, response: dict, log_path: Path = EGRESS_LOG) -> None:
    raw = payload_json(payload)
    record = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "destination": destination,
        "payload_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "payload": json.loads(raw),
        "response": response,
    }
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
