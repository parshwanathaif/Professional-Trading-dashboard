"""Integration tests: paper trading persistence/idempotency, scheduler locks/restart, audit trail, reports."""

import json
import os
import time

import pandas as pd
import pytest

from bql.config import load_config
from bql.paper_trading import PaperLedger, PaperTrader
from bql.paper_trading.ledger import PaperLedger as _L
from bql.reporting import build_period_report
from bql.scheduler import RunRegistry, WorkflowLocked, daily_workflow, file_lock, run_workflow
from bql.scheduler.workflows import WorkflowStopped


def _test_mode(ws):
    import yaml
    p = ws / "config" / "settings.yaml"
    s = yaml.safe_load(p.read_text())
    s["paper_trading"]["test_mode_allow_unvalidated"] = True
    p.write_text(yaml.safe_dump(s, sort_keys=False))
    return load_config(ws / "config", ws)


def _counts(ledger, name):
    return {t: int(ledger.query(f"SELECT COUNT(*) n FROM {t} WHERE portfolio=?", (name,))["n"].iloc[0])
            for t in ("signals", "fills", "trades", "equity", "order_events")}


def test_paper_requires_validated_strategies(ws_cfg):
    out = PaperTrader(ws_cfg).run(start="2020-06-01", as_of="2020-12-31")
    led = PaperLedger(ws_cfg.path("paper_db"))
    assert out["test_mode"] is False and not any(out["strategy_permissions"].values())
    assert _counts(led, "test_paper")["fills"] == 0
    rej = led.query("SELECT detail FROM signals WHERE decision='REJECTED'")
    assert rej["detail"].str.contains("research status").any()


def test_paper_run_idempotent_and_audited(workspace):
    cfg = _test_mode(workspace)
    pt = PaperTrader(cfg)
    out = pt.run(start="2020-06-01", as_of="2020-12-31")
    assert out["status"] == "OK" and out["test_mode"]
    led = pt.ledger
    c1 = _counts(led, "test_paper")
    assert c1["fills"] > 0 and c1["equity"] == len(out["sessions_processed"])
    again = PaperTrader(cfg).run(as_of="2020-12-31")
    assert again["status"] == "UP_TO_DATE"
    assert _counts(led, "test_paper") == c1
    # audit trail completeness
    fills = led.query("SELECT * FROM fills")
    assert fills["order_id"].notna().all() or fills["reason"].str.contains("STOP|WRITE_OFF").any()
    assert (fills["test_mode"] == 1).all()
    sig = led.query("SELECT * FROM signals WHERE decision='ORDER'")
    assert len(sig) and sig["reason"].str.len().gt(0).all() and sig["indicators_json"].str.len().gt(2).all()
    tr = led.query("SELECT * FROM trades")
    if len(tr):
        assert tr["exit_reason"].notna().all() and tr["entry_reason"].notna().all()
    assert len(led.query("SELECT * FROM audit_log WHERE event='SESSION_PROCESSED'")) == c1["equity"]
    # continue later: only new sessions processed, state carried over
    nxt = PaperTrader(cfg).run(as_of="2021-01-15")
    assert nxt["status"] == "OK" and all(d > "2020-12-31" for d in nxt["sessions_processed"])


def test_paper_restart_after_crash_no_duplicates(workspace, monkeypatch):
    cfg = _test_mode(workspace)
    real = _L.commit_session
    calls = {"n": 0}

    def flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 5:
            raise RuntimeError("simulated crash")
        return real(self, *a, **k)

    monkeypatch.setattr(_L, "commit_session", flaky)
    with pytest.raises(RuntimeError):
        PaperTrader(cfg).run(start="2020-09-01", as_of="2020-10-30")
    monkeypatch.setattr(_L, "commit_session", real)
    led = PaperLedger(cfg.path("paper_db"))
    assert len(led.query("SELECT * FROM processed_sessions")) == 4
    PaperTrader(cfg).run(as_of="2020-10-30")
    sess = led.query("SELECT session_date FROM processed_sessions")
    assert sess["session_date"].is_unique
    eq = led.query("SELECT date FROM equity")
    assert eq["date"].is_unique and len(eq) == len(sess)


def test_kill_switch_persists_and_manual_reset(workspace):
    cfg = _test_mode(workspace)
    pt = PaperTrader(cfg)
    pt.run(start="2020-06-01", as_of="2020-07-31")
    sd, state = pt.ledger.load_snapshot("test_paper")
    state["risk"].update(kill_switch=True, kill_reason="test breach", kill_date=sd)
    with pt.ledger.tx() as con:
        con.execute("UPDATE state_snapshots SET state_json=? WHERE session_date=?", (json.dumps(state), sd))
    out = PaperTrader(cfg).run(as_of="2020-08-31")
    assert out["kill_switch"] is True
    new_orders = pt.ledger.query("SELECT * FROM signals WHERE signal_date>? AND decision='ORDER'", (sd,))
    assert new_orders.empty
    assert pt.reset_kill_switch("analyst", "reviewed")["status"] == "RESET"
    assert not pt.ledger.load_snapshot("test_paper")[1]["risk"]["kill_switch"]
    assert len(pt.ledger.query("SELECT * FROM audit_log WHERE event='KILL_SWITCH_RESET'")) == 1


def test_file_lock_blocks_concurrent_and_recovers_stale(tmp_path):
    lock = tmp_path / "x.lock"
    with file_lock(lock, 60):
        with pytest.raises(WorkflowLocked):
            with file_lock(lock, 60):
                pass
    lock.write_text(json.dumps({"pid": 1, "ts": time.time() - 3600 * 5}))
    with file_lock(lock, 60):
        assert lock.exists()
    assert not lock.exists()


def test_workflow_duplicate_protection_and_resume(ws_cfg):
    calls = []

    def ok(ctx):
        calls.append("a")

    state = {"fail": True}

    def flaky(ctx):
        calls.append("b")
        if state["fail"]:
            raise RuntimeError("boom")

    steps = [("a", ok), ("b", flaky)]
    r1 = run_workflow(ws_cfg, "t", "t:1", steps)
    assert r1["status"] == "FAILED" and calls == ["a", "b"]
    state["fail"] = False
    r2 = run_workflow(ws_cfg, "t", "t:1", steps)
    assert r2["status"] == "COMPLETED" and calls == ["a", "b", "b"]   # resumed after completed step 'a'
    r3 = run_workflow(ws_cfg, "t", "t:1", steps)
    assert r3["status"] == "SKIPPED_ALREADY_COMPLETED"
    hist = RunRegistry(ws_cfg.path("state_dir") / "workflow_runs.sqlite").get("t:1")
    assert hist["attempts"] == 2


def test_workflow_stop_is_recorded(ws_cfg):
    def stop(ctx):
        raise WorkflowStopped("data not yet available")

    r = run_workflow(ws_cfg, "t2", "t2:1", [("s", stop)])
    assert r["status"] == "STOPPED"
    assert json.loads((ws_cfg.path("state_dir") / "last_run_status.json").read_text())["status"] == "STOPPED"


def test_daily_workflow_non_trading_day_and_missing_data(ws_cfg):
    r = daily_workflow(ws_cfg, as_of="2021-06-05")   # Saturday
    assert r["status"] == "COMPLETED_NO_SESSION"
    r2 = daily_workflow(ws_cfg, as_of="2021-07-30")  # after last data -> must stop, not trade
    assert r2["status"] == "STOPPED" and "not yet available" in r2["error"]


def test_daily_workflow_end_to_end_and_reports(workspace):
    cfg = _test_mode(workspace)
    PaperTrader(cfg).run(start="2021-03-01", as_of="2021-05-31")
    r = daily_workflow(cfg, as_of="2021-06-01")
    assert r["status"] == "COMPLETED", r
    again = daily_workflow(cfg, as_of="2021-06-01")
    assert again["status"] == "SKIPPED_ALREADY_COMPLETED"
    for period in ("weekly", "monthly", "annual"):
        paths = build_period_report(PaperLedger(cfg.path("paper_db")), "test_paper", period, cfg.path("reports_dir"),
                                    None, None, True)
        for k in ("md", "html", "xlsx", "pdf"):
            assert os.path.getsize(paths[k]) > 500
    md = paths["md"].read_text()
    assert "SYNTHETIC" in md and "PAPER TRADING ONLY" in md


def test_dq_gate_blocks_paper_when_benchmark_missing(workspace):
    import duckdb
    cfg = _test_mode(workspace)
    con = duckdb.connect(str(cfg.path("market_db")))
    con.execute("DELETE FROM benchmarks")
    con.close()
    from bql.paper_trading import PaperTradingHalted
    with pytest.raises(PaperTradingHalted):
        PaperTrader(cfg).run(as_of="2021-01-15")
    assert PaperLedger(cfg.path("paper_db")).last_session("test_paper") is None
    assert pd.Timestamp("2021-01-15")
