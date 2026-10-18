"""PoC cloud analyst: Claude designs and interprets a weight chart from anonymized monthly aggregates.

    python -m privacy.cloud_chart            # preview -> approve -> Claude -> data/cloud_weight.png + .md

Only the privacy gateway's payload is sent (monthly mean/min/max/days, no text). Claude returns a
*chart plan* as schema-validated JSON (title, phases, annotations, insights); the chart itself is
rendered locally with matplotlib. No code from the cloud is executed.

Needs an Anthropic API key: ANTHROPIC_API_KEY, or it is asked for (hidden) and not stored.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sqlite3
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .gateway import DATA, WeightPayload, build_weight_payload, confirm, log_egress, payload_json, preview

MODEL = "claude-opus-5-5"
DESTINATION = f"Anthropic Claude API ({MODEL})"

# ---- chart plan returned by Claude (validated before use) -------------------------------------

MONTH = r"^(19|20)\d{2}-(0[1-9]|1[0-2])$"


class Annotation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    month: str = Field(pattern=MONTH)
    label: str = Field(max_length=60)


class Phase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str = Field(pattern=MONTH)
    end: str = Field(pattern=MONTH)
    label: str = Field(max_length=40)


class ChartPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(max_length=80)
    subtitle: str = Field(max_length=120)
    start_month: str = Field(pattern=MONTH)   # where the x-axis starts (skip sparse early years if sensible)
    rolling_months: int = Field(ge=3, le=24)
    show_min_max_band: bool
    phases: list[Phase] = Field(max_length=6)
    annotations: list[Annotation] = Field(max_length=6)
    insights: list[str] = Field(max_length=6)


PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "subtitle", "start_month", "rolling_months", "show_min_max_band",
                 "phases", "annotations", "insights"],
    "properties": {
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "start_month": {"type": "string", "description": "YYYY-MM"},
        "rolling_months": {"type": "integer", "enum": [3, 6, 12, 24]},
        "show_min_max_band": {"type": "boolean"},
        "phases": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["start", "end", "label"],
            "properties": {"start": {"type": "string"}, "end": {"type": "string"}, "label": {"type": "string"}}}},
        "annotations": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["month", "label"],
            "properties": {"month": {"type": "string"}, "label": {"type": "string"}}}},
        "insights": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM = (
    "You are a careful data-visualization analyst. You receive monthly aggregates of one person's body "
    "weight in kg (mean, min, max, number of measured days per month). Design one clear line chart and "
    "describe what the data shows. Answer in German. Keep labels short (titles <= 80 chars, phase labels "
    "<= 40, annotation labels <= 60). Use at most 4 phases, 4 annotations and 5 insights. Phases mark "
    "multi-month periods with a distinct level or trend; annotations mark single notable months. Base "
    "every statement only on the numbers; mention where data is sparse; do not give medical advice or "
    "judge the person; describe, don't evaluate."
)


def request_plan(payload: WeightPayload, api_key: str | None = None) -> tuple[ChartPlan, dict]:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    request = dict(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
        system=SYSTEM,
        messages=[{"role": "user", "content": "Monthly aggregates (JSON):\n" + payload_json(payload)}],
    )
    try:
        response = client.beta.messages.create(
            **request, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    except anthropic.BadRequestError as e:
        if "fallback" not in e.message.lower():
            raise
        print(f"Note: refusal fallback not accepted ({e.message}); retrying once without it.")
        response = client.beta.messages.create(**request)
    meta = {"id": response.id, "model": response.model, "stop_reason": response.stop_reason,
            "input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
    if response.stop_reason == "refusal":
        raise SystemExit(f"Claude declined the request: {response.stop_details}")
    if response.stop_reason == "max_tokens":
        raise SystemExit("The answer was cut off (max_tokens). Try again.")
    text = next(b.text for b in response.content if b.type == "text")
    meta["plan"] = json.loads(text)
    return ChartPlan.model_validate(meta["plan"]), meta


# ---- local rendering --------------------------------------------------------------------------

SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
BLUE, BLUE_BAND, ORANGE, PHASE = "#2a78d6", "#cde2fb", "#eb6834", "#f0efec"


def _month_date(m: str) -> date:
    y, mo = m.split("-")
    return date(int(y), int(mo), 15)


def rolling(months: list[str], values: list[float], window: int) -> list[float | None]:
    """Mean over the last `window` calendar months that have data; None if fewer than 3 of them."""
    out = []
    idx = {m: v for m, v in zip(months, values)}
    for m in months:
        d = _month_date(m)
        win = [v for k, v in idx.items() if 0 <= (d - _month_date(k)).days < window * 30.5]
        out.append(sum(win) / len(win) if len(win) >= 3 else None)
    return out


def render(payload: WeightPayload, plan: ChartPlan, out_png: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = [r for r in payload.months if r.month >= plan.start_month] or list(payload.months)
    months = [r.month for r in rows]
    x = [_month_date(m) for m in months]
    mean = [r.mean for r in rows]
    roll = rolling(months, mean, plan.rolling_months)

    fig, ax = plt.subplots(figsize=(11, 5.6), dpi=160)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    for ph in plan.phases:
        ax.axvspan(_month_date(ph.start), _month_date(ph.end), color=PHASE, zorder=0, lw=0)
        ax.text(_month_date(ph.start), 1.0, " " + ph.label, transform=ax.get_xaxis_transform(),
                va="bottom", ha="left", fontsize=8.5, color=INK_2)
    if plan.show_min_max_band:
        ax.fill_between(x, [r.min for r in rows], [r.max for r in rows], color=BLUE_BAND, lw=0,
                        zorder=1, label="Monats-Min/Max")
    ax.plot(x, mean, color=BLUE, lw=0, marker="o", ms=3.6, zorder=3, label="Monatsmittel")
    rx = [d for d, v in zip(x, roll) if v is not None]
    ry = [v for v in roll if v is not None]
    ax.plot(rx, ry, color=ORANGE, lw=2, zorder=4, label=f"{plan.rolling_months}-Monats-Mittel")
    if rx:
        ax.annotate(f"{ry[-1]:.1f} kg", (rx[-1], ry[-1]), xytext=(6, 0), textcoords="offset points",
                    va="center", fontsize=9, color=INK)

    by_month = {r.month: r.mean for r in rows}
    for an in plan.annotations:
        if an.month in by_month:
            px, py = _month_date(an.month), by_month[an.month]
            ax.annotate(an.label, (px, py), xytext=(0, 18), textcoords="offset points", ha="center",
                        fontsize=8.5, color=INK_2,
                        arrowprops={"arrowstyle": "-", "color": INK_2, "lw": 0.8})

    fig.text(0.012, 0.975, plan.title, fontsize=14, color=INK, fontweight="bold", va="top")
    fig.text(0.012, 0.915, plan.subtitle, fontsize=9.5, color=INK_2, va="top")
    ax.set_ylabel("kg", color=INK_2)
    ax.grid(axis="y", color=GRID, lw=0.8)
    ax.tick_params(colors=INK_2, labelsize=9)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.legend(loc="lower left", frameon=False, fontsize=9, labelcolor=INK_2)
    fig.tight_layout(rect=(0, 0, 1, 0.86))  # leave room for title, subtitle and phase labels
    fig.savefig(out_png, facecolor=SURFACE)
    plt.close(fig)


def write_insights(plan: ChartPlan, meta: dict, out_md: Path) -> None:
    lines = [f"# {plan.title}", "", plan.subtitle, "", "## Beobachtungen (Claude)", ""]
    lines += [f"- {s}" for s in plan.insights]
    lines += ["", f"_Modell: {meta.get('model')} · Tokens: {meta.get('input_tokens')} in / "
                  f"{meta.get('output_tokens')} out · gesendet wurden nur Monatsaggregate._"]
    out_md.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DATA / "corpus.db")
    ap.add_argument("--out", type=Path, default=DATA / "cloud_weight")
    args = ap.parse_args(argv)
    if not (DATA / ".vault").exists() and args.db.parent == DATA:
        raise SystemExit("The vault is not open (scripts\\vault-open.ps1).")

    payload = build_weight_payload(sqlite3.connect(str(args.db)))
    print(preview(payload, DESTINATION))
    if not confirm():
        print("Cancelled - nothing was sent.")
        return

    import anthropic

    api_key = (os.environ.get("ANTHROPIC_API_KEY")
               or getpass.getpass("Anthropic API key (not stored, paste with right-click): ")).strip()
    if not api_key.startswith("sk-ant-") or any(ord(c) < 32 for c in api_key):
        raise SystemExit(f"That does not look like an Anthropic API key (got {len(api_key)} characters, expected"
                         " 'sk-ant-...'). Paste it with a right-click, not Ctrl+V. Nothing was sent.")
    try:
        plan, meta = request_plan(payload, api_key)
    except anthropic.AuthenticationError:
        raise SystemExit("API key rejected (401). Check the key at console.anthropic.com. Nothing was processed.")
    except anthropic.RateLimitError as e:
        log_egress(DESTINATION, payload, {"error": "rate_limit", "status": e.status_code})
        raise SystemExit("Rate limit reached (429). Wait a minute and try again.")
    except anthropic.APIStatusError as e:
        # the request reached Anthropic, so it is logged even though it was rejected
        log_egress(DESTINATION, payload, {"error": e.type, "status": e.status_code, "message": e.message})
        hint = (" Top up credits under console.anthropic.com -> Billing." if "credit" in e.message.lower() else "")
        raise SystemExit(f"Claude API error {e.status_code} ({e.type}): {e.message}{hint}")
    except anthropic.APIConnectionError:
        raise SystemExit("No connection to the Claude API (network or firewall). Nothing was sent.")
    log_egress(DESTINATION, payload, meta)

    png, md = args.out.with_suffix(".png"), args.out.with_suffix(".md")
    render(payload, plan, png)
    write_insights(plan, meta, md)
    print(f"\nChart:    {png}\nInsights: {md}\nLogged:   {DATA / 'egress.log'}")
    print("\n" + "\n".join(f"- {s}" for s in plan.insights))


if __name__ == "__main__":
    main()
