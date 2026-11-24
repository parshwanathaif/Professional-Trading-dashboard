"""Research engine integration test on a small synthetic workspace (no alpha embedded)."""

import json

from bql.pipeline import run_research
from bql.validation import STATUSES


def test_research_pipeline_end_to_end(ws_cfg):
    res = run_research(ws_cfg, ["trend_ma"])
    out = res["out_dir"]
    ev = json.loads((out / "trend_ma" / "evaluation.json").read_text())
    assert ev["status"] in STATUSES
    # synthetic data can never be promoted beyond "Insufficient evidence"
    assert ev["status"] in ("Rejected", "Insufficient evidence")
    assert ev["leakage"]["passed"]
    sp = ev["splits"]
    assert sp["train"]["end"] < sp["validation"]["start"] and sp["validation"]["end"] < sp["test"]["start"]
    for f in ev["walk_forward"]["folds"]:
        assert f["test"].split("..")[1] <= sp["validation"]["end"]   # WF never touches the final test period
    assert {"costs_x2", "slippage_x2", "delay_2_bars", "liquidity_participation_1pct"} <= set(ev["robustness"])
    assert "dsr" in ev["statistics"]["deflated_sharpe"] and "pbo" in ev["statistics"]["pbo"]
    for f in ("report.md", "report.html", "report.pdf", "test_trades.csv", "curves.parquet"):
        assert (out / "trend_ma" / f).exists()
    md = (out / "trend_ma" / "report.md").read_text()
    for i in ("1. Research hypothesis", "4. Backtest assumptions", "8. Statistical validation", "9. Weaknesses",
              "10. Market conditions", "12. Final research status"):
        assert i in md
    log = (ws_cfg.path("reports_dir") / "research" / "test_evaluations.jsonl").read_text().strip().splitlines()
    assert len(log) == 1
