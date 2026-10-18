import json
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from analysis.measurements import load_kinds, store_all
from ingest.segment import Entry
from ingest.store import connect, sync_entries
from privacy.cloud_chart import ChartPlan, render, rolling, write_insights
from privacy.gateway import MonthlyStat, WeightPayload, build_weight_payload, log_egress, preview

ROW = {"month": "2024-03", "mean": 70.1, "min": 69.0, "max": 71.0, "days": 5}


def test_gateway_rejects_anything_but_numbers():
    MonthlyStat(**ROW)
    with pytest.raises(ValidationError):            # unknown field (e.g. a note) is refused
        MonthlyStat(**ROW, note="beim Arzt")
    with pytest.raises(ValidationError):            # text smuggled into the month
        MonthlyStat(**{**ROW, "month": "2024-03 Lena"})
    with pytest.raises(ValidationError):            # exact day instead of month
        MonthlyStat(**{**ROW, "month": "2024-03-12"})
    with pytest.raises(ValidationError):            # string where a number belongs (strict mode)
        MonthlyStat(**{**ROW, "mean": "70.1"})
    with pytest.raises(ValidationError):
        WeightPayload(months=(), kind="body_weight_monthly_kg")
    with pytest.raises(ValidationError):
        WeightPayload(months=(MonthlyStat(**ROW),), kind="journal_text")


def test_payload_from_db_has_only_monthly_aggregates(tmp_path):
    cfg = tmp_path / "m.yaml"
    cfg.write_text("measurements:\n  weight:\n    unit: kg\n    min: 30\n    max: 250\n    patterns:\n"
                   "      - '(?i)gewicht:\\s*(?P<value>\\d{2,3}(?:[.,]\\d)?)\\s*kg'\n", encoding="utf-8")
    con = connect(tmp_path / "t.db")
    start = date(2024, 1, 1)
    entries = [Entry(start + timedelta(days=i), start + timedelta(days=i), "day", "x", i, "plain",
                     [f"Geheimer Text {i}. Gewicht: {70 + (i % 3) * 0.5:.1f} kg"]) for i in range(60)]
    entries[30].paragraphs[0] = "Tippfehler. Gewicht: 37,0 kg"
    sync_entries(con, entries)
    store_all(con, load_kinds(cfg))

    payload = build_weight_payload(con)
    raw = payload.model_dump_json()
    assert [m.month for m in payload.months] == ["2024-01", "2024-02"]
    assert "Geheim" not in raw and "37" not in raw   # no text, and the typo outlier stays home
    assert "rows:   2 months" in preview(payload, "test")

    log = tmp_path / "egress.log"
    log_egress("test", payload, {"id": "msg_1"}, log)
    rec = json.loads(log.read_text(encoding="utf-8"))
    assert rec["destination"] == "test" and rec["payload"]["kind"] == "body_weight_monthly_kg"


def test_plan_validation_and_local_render(tmp_path):
    months = [f"{y}-{m:02d}" for y in range(2019, 2027) for m in range(1, 13)][:94]
    payload = WeightPayload(months=tuple(
        MonthlyStat(month=mo, mean=72 - i * 0.03 + (i % 5) * 0.2, min=70.5 - i * 0.03, max=73.5 - i * 0.03,
                    days=4 + i % 10) for i, mo in enumerate(months)))
    plan = ChartPlan.model_validate({
        "title": "Gewicht 2019–2026", "subtitle": "Monatsmittel mit 12-Monats-Mittel (synthetische Testdaten)",
        "start_month": "2019-01", "rolling_months": 12, "show_min_max_band": True,
        "phases": [{"start": "2020-03", "end": "2021-06", "label": "Plateau"}],
        "annotations": [{"month": "2023-07", "label": "Tiefster Monat"}],
        "insights": ["Leichter Abwärtstrend.", "Dichte Daten ab 2020."],
    })
    with pytest.raises(ValidationError):
        ChartPlan.model_validate({**plan.model_dump(), "start_month": "irgendwann"})

    png = tmp_path / "w.png"
    render(payload, plan, png)
    assert png.stat().st_size > 10_000
    write_insights(plan, {"model": "test"}, tmp_path / "w.md")
    assert "Leichter Abwärtstrend" in (tmp_path / "w.md").read_text(encoding="utf-8")


def test_rolling_needs_three_months():
    assert rolling(["2024-01", "2024-02", "2024-03"], [70, 71, 72], 12) == [None, None, 71.0]
