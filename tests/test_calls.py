"""Daily BUY/HOLD/SELL calls and evidence-based strategy selection."""

import json

import pandas as pd
import yaml

from bql.calls import build_calls, render_daily, render_markdown, write_calls
from bql.config import SelectionCfg, load_config
from bql.paper_trading import PaperTrader
from bql.paper_trading.engine import research_parameters, select_best_strategies, select_shadow_strategies


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
    assert "Validated calls for" in md and "TEST MODE" in md
    assert "SEBI" in render_daily(c, None)
    paths = write_calls(cfg, allowed)
    assert paths["latest"].exists() and json.loads(paths["json"].read_text())["next_session"] == c["next_session"]


def _eval_stub(root, name, passed=True, params=None):
    d = root / "research" / "r1" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "evaluation.json").write_text(json.dumps({"strategy": name, "leakage": {"passed": passed},
                                                   "selected_params": params or {}}))


def test_shadow_picks_top_score_regardless_of_status_but_never_leaky(tmp_path):
    _research_stub(tmp_path, [{"strategy": "a", "status": "Rejected", "score": 90},
                              {"strategy": "b", "status": "Rejected", "score": 70},
                              {"strategy": "c", "status": "Needs further validation", "score": 50}])
    _eval_stub(tmp_path, "a", passed=False, params={"x": 1})
    _eval_stub(tmp_path, "b", params={"fast": 50, "slow": 250})
    _eval_stub(tmp_path, "c")
    from bql.config import ShadowCfg
    assert select_shadow_strategies(tmp_path / "research", ShadowCfg(enabled=True, max_strategies=1)) == ["b"]
    assert research_parameters(tmp_path / "research")["b"] == {"fast": 50, "slow": 250}


def test_shadow_book_is_separate_and_tagged(workspace):
    from bql.pipeline import run_research

    p = workspace / "config" / "settings.yaml"
    s = yaml.safe_load(p.read_text())
    s["paper_trading"]["shadow"] = {"enabled": True, "portfolio_name": "shadow_book", "max_strategies": 1}
    p.write_text(yaml.safe_dump(s, sort_keys=False))
    cfg = load_config(workspace / "config", workspace)
    run_research(cfg, ["trend_ma"])
    main_out = PaperTrader(cfg).run(start="2020-09-01", as_of="2021-01-29")
    sh = PaperTrader(cfg, shadow=True)
    out = sh.run(start="2020-09-01", as_of="2021-01-29")
    assert out["portfolio"] == "shadow_book" and out["shadow"] and out["test_mode"]
    assert out["strategy_permissions"]["trend_ma"] and not any(main_out["strategy_permissions"].values())
    fills = sh.ledger.query("SELECT DISTINCT portfolio, test_mode FROM fills")
    assert set(fills["portfolio"]) <= {"shadow_book"} and (fills["test_mode"] == 1).all()
    paths = write_calls(cfg, PaperTrader(cfg)._strategy_permissions()[0], sh._strategy_permissions()[0])
    md = paths["latest"].read_text()
    assert "Validated calls for" in md and "SHADOW calls for" in md and "TRACK ONLY" in md
