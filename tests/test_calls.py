"""Daily BUY/HOLD/SELL calls and evidence-based strategy selection."""

import json

import pandas as pd
import yaml

from bql.calls import build_calls, render_markdown, write_calls
from bql.config import SelectionCfg, load_config
from bql.paper_trading import PaperTrader
from bql.paper_trading.engine import select_best_strategies


def _research_stub(root, rows):
    run = root / "research" / "r1"
    run.mkdir(parents=True)
    pd.DataFrame(rows).to_csv(run / "summary.csv", index=False)
    (root / "research" / "latest.json").write_text(json.dumps({"run_id": "r1", "statuses": {r["strategy"]: r["status"]
                                                                                          for r in rows}}))


def test_best_evidence_never_selects_rejected(tmp_path):
    _research_stub(tmp_path, [
        {"strategy": "a", "status": "Rejected", "score": 90},
        {"strategy": "b", "status": "Needs further validation", "score": 50},
        {"strategy": "c", "status": "Passed historical validation", "score": 60},
        {"strategy": "d", "status": "Insufficient evidence", "score": 80},
    ])
    sel = SelectionCfg(mode="best_evidence", max_strategies=2, min_status="Needs further validation")
    assert select_best_strategies(tmp_path / "research", sel) == ["c", "b"]
    assert select_best_strategies(tmp_path / "research", SelectionCfg(mode="status_only")) == []
    only_rejected = tmp_path / "x"
    _research_stub(only_rejected, [{"strategy": "a", "status": "Rejected", "score": 99}])
    assert select_best_strategies(only_rejected / "research", sel) == []


def test_calls_generated_from_model_portfolio(workspace):
    p = workspace / "config" / "settings.yaml"
    s = yaml.safe_load(p.read_text())
    s["paper_trading"]["test_mode_allow_unvalidated"] = True
    p.write_text(yaml.safe_dump(s, sort_keys=False))
    cfg = load_config(workspace / "config", workspace)
    pt = PaperTrader(cfg)
    pt.run(start="2020-09-01", as_of="2021-03-31")
    allowed, _ = pt._strategy_permissions()
    c = build_calls(cfg, allowed)
    assert c["session"] == "2021-03-31" and c["next_session"] > c["session"]
    assert c["test_mode"] is True
    for b in c["buy"]:
        assert b["stop_loss"] < b["last_close"] and 0 < b["weight_pct"] <= 8.01
    held = {h["symbol"] for h in c["hold"]}
    assert not held & {s["symbol"] for s in c["sell"]}
    md = render_markdown(c)
    assert "Trading calls for" in md and "TEST MODE" in md and "SEBI" in md
    paths = write_calls(cfg, allowed)
    assert paths["latest"].exists() and json.loads(paths["json"].read_text())["next_session"] == c["next_session"]
