"""Automated research workflows with locking, duplicate-run protection and restart capability.

* A file lock (`state/locks/<workflow>.lock`) prevents concurrent runs; a lock older than
  `scheduler.lock_stale_after_minutes` is treated as abandoned (crash) and taken over (logged).
* Every run has a key (workflow + session date). A completed key is skipped unless `force`.
* Steps are recorded as they complete; a failed/interrupted run resumes after its last
  completed step. Every step is itself idempotent (imports dedupe, paper sessions commit once).
* Failure in any step stops the run (no paper trades from unvalidated/incomplete data) and the
  error is written to `state/last_run_status.json`, shown on the dashboard.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
import traceback
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pandas as pd

from .. import pipeline
from ..config import AppConfig
from ..data import MarketStore
from ..logging_setup import get_logger, log_event, now_ist
from ..market_calendar import TradingCalendar
from ..paper_trading import PaperLedger, PaperTrader
from ..reporting import build_period_report
from ..reporting.periodic import load_portfolio_frames

log = get_logger("scheduler")


class WorkflowLocked(RuntimeError):
    pass


class WorkflowStopped(RuntimeError):
    """Raised by a step to stop the run deliberately (e.g. data not yet available)."""


@contextmanager
def file_lock(path: Path, stale_minutes: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "ts": time.time()})
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            info = json.loads(path.read_text() or "{}")
        except (OSError, json.JSONDecodeError):
            info = {}
        age_min = (time.time() - float(info.get("ts", 0))) / 60
        if age_min < stale_minutes:
            raise WorkflowLocked(f"workflow already running (lock {path}, age {age_min:.0f} min)") from None
        log_event(log, "taking over stale lock", level=30, lock=str(path), age_min=round(age_min, 1))
        path.unlink(missing_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, payload.encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


class RunRegistry:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._con() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS workflow_runs (
                run_key TEXT PRIMARY KEY, workflow TEXT, status TEXT, steps_done TEXT, started_ist TEXT,
                finished_ist TEXT, error TEXT, attempts INTEGER)""")

    @contextmanager
    def _con(self):
        c = sqlite3.connect(self.path, timeout=30)
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def get(self, key: str) -> dict | None:
        with self._con() as c:
            r = c.execute("SELECT run_key, workflow, status, steps_done, started_ist, finished_ist, error, attempts "
                          "FROM workflow_runs WHERE run_key=?", (key,)).fetchone()
        if not r:
            return None
        return dict(zip(["run_key", "workflow", "status", "steps_done", "started_ist", "finished_ist", "error",
                         "attempts"], r)) | {"steps_done": json.loads(r[3] or "[]")}

    def start(self, key: str, workflow: str) -> dict:
        prev = self.get(key)
        with self._con() as c:
            if prev is None:
                c.execute("INSERT INTO workflow_runs VALUES (?,?,?,?,?,?,?,?)",
                          (key, workflow, "RUNNING", "[]", now_ist().isoformat(timespec="seconds"), None, None, 1))
            else:
                c.execute("UPDATE workflow_runs SET status='RUNNING', attempts=attempts+1, error=NULL WHERE run_key=?",
                          (key,))
        return self.get(key)

    def step_done(self, key: str, step: str) -> None:
        r = self.get(key)
        steps = r["steps_done"] + [step]
        with self._con() as c:
            c.execute("UPDATE workflow_runs SET steps_done=? WHERE run_key=?", (json.dumps(steps), key))

    def finish(self, key: str, status: str, error: str | None = None) -> None:
        with self._con() as c:
            c.execute("UPDATE workflow_runs SET status=?, finished_ist=?, error=? WHERE run_key=?",
                      (status, now_ist().isoformat(timespec="seconds"), error, key))

    def history(self, limit: int = 50) -> pd.DataFrame:
        c = sqlite3.connect(self.path)
        try:
            return pd.read_sql_query("SELECT * FROM workflow_runs ORDER BY started_ist DESC LIMIT ?", c, params=(limit,))
        finally:
            c.close()


def _status_file(cfg: AppConfig) -> Path:
    return cfg.path("state_dir") / "last_run_status.json"


def run_workflow(cfg: AppConfig, workflow: str, key: str, steps: list[tuple[str, Callable[[dict], None]]],
                 force: bool = False) -> dict:
    reg = RunRegistry(cfg.path("state_dir") / "workflow_runs.sqlite")
    prev = reg.get(key)
    if prev and prev["status"] in ("COMPLETED", "COMPLETED_NO_SESSION") and not force:
        log_event(log, "duplicate run skipped", workflow=workflow, key=key)
        return {"workflow": workflow, "key": key, "status": "SKIPPED_ALREADY_COMPLETED"}
    if force and prev:
        with reg._con() as c:
            c.execute("UPDATE workflow_runs SET steps_done='[]' WHERE run_key=?", (key,))
        prev = reg.get(key)
    ctx: dict = {"messages": []}
    status, err = "COMPLETED", None
    with file_lock(cfg.path("state_dir") / "locks" / f"{workflow}.lock", cfg.settings.scheduler.lock_stale_after_minutes):
        rec = reg.start(key, workflow)
        done = set(rec["steps_done"])
        for name, fn in steps:
            if name in done:
                ctx["messages"].append(f"{name}: already completed (resumed run)")
                continue
            try:
                fn(ctx)
                reg.step_done(key, name)
                ctx["messages"].append(f"{name}: ok")
                if ctx.get("skip_rest"):
                    status = "COMPLETED_NO_SESSION"
                    ctx["messages"].append(ctx["skip_rest"])
                    break
            except WorkflowStopped as e:
                status, err = "STOPPED", f"{name}: {e}"
                break
            except Exception as e:  # noqa: BLE001 - every failure must be recorded, not swallowed
                status, err = "FAILED", f"{name}: {e}\n{traceback.format_exc(limit=5)}"
                break
        reg.finish(key, status, err)
    result = {"workflow": workflow, "key": key, "status": status, "error": err, "messages": ctx["messages"],
              "finished_ist": now_ist().isoformat(timespec="seconds")}
    _status_file(cfg).write_text(json.dumps(result, indent=2, default=str))
    log_event(log, "workflow finished", level=20 if status == "COMPLETED" else 40, **result)
    return result


# =========================================================================== daily
def daily_workflow(cfg: AppConfig, as_of=None, force: bool = False) -> dict:
    store_path = cfg.path("market_db")
    session = pd.Timestamp(as_of).normalize() if as_of else pd.Timestamp(now_ist().date())

    def check_calendar(ctx):
        cal = TradingCalendar.from_store(MarketStore(store_path, read_only=True))
        ctx["calendar_verified"] = cal.holiday_list_verified(session.year)
        if not cal.is_trading_day(session):
            ctx["skip_rest"] = f"{session.date()} is not a trading day (weekend/holiday): nothing to do"
            return
        if as_of is None and not cal.session_closed():
            raise WorkflowStopped("market session has not closed yet (IST)")

    def import_data(ctx):
        X = cfg.settings.exchange_data
        if X.enabled and X.auto_fetch_in_daily_workflow:
            # official NSE/BSE archive files for the session (resumable, rate-limited, incremental)
            ctx["exchange_fetch"] = pipeline.fetch_exchange_data(cfg, start=session - pd.Timedelta(days=10),
                                                                 end=session)
        ctx["imports"] = pipeline.import_inbox(cfg)

    def validate(ctx):
        rep, paths = pipeline.data_quality(cfg, "daily", as_of=session)
        ctx["dq"] = rep.summary
        if rep.blocked:
            raise WorkflowStopped(f"data-quality gate failed; see {paths['html']}")

    def availability(ctx):
        store = MarketStore(store_path, read_only=True)
        last = store.last_price_date()
        bench = store.benchmark(cfg.settings.backtest.benchmark)
        if last is None or last < session or bench.empty or bench["date"].max() < session:
            raise WorkflowStopped(f"data for {session.date()} not yet available (latest prices {last}, "
                                  f"benchmark {bench['date'].max() if not bench.empty else None})")

    def paper(ctx):
        ctx["paper"] = PaperTrader(cfg).run(as_of=session)

    def report(ctx):
        ledger = PaperLedger(cfg.path("paper_db"))
        store = MarketStore(store_path, read_only=True)
        b = store.benchmark(cfg.settings.backtest.benchmark).set_index("date")["close"]
        ctx["report"] = build_period_report(ledger, cfg.settings.paper_trading.portfolio_name, "daily",
                                            cfg.path("reports_dir"), b, session, store.any_synthetic(),
                                            cfg.settings.backtest.risk_free_rate_annual)

    steps = [("check_calendar", check_calendar), ("import_new_data", import_data), ("validate_data", validate),
             ("check_data_availability", availability),
             ("indicators_regime_signals_risk_paper_update", paper), ("daily_report", report)]
    return run_workflow(cfg, "daily", f"daily:{session.date()}", steps, force)


# =========================================================================== weekly
def strategy_degradation(cfg: AppConfig) -> pd.DataFrame:
    """Compare paper-trading trade outcomes per strategy with the latest research expectations."""
    ledger = PaperLedger(cfg.path("paper_db"))
    trades = ledger.query("SELECT * FROM trades WHERE portfolio=?", (cfg.settings.paper_trading.portfolio_name,))
    root = cfg.path("reports_dir") / "research"
    latest = root / "latest.json"
    rows = []
    if not latest.exists():
        return pd.DataFrame()
    run_id = json.loads(latest.read_text())["run_id"]
    for name in cfg.strategies.strategies:
        ev_path = root / run_id / name / "evaluation.json"
        if not ev_path.exists():
            continue
        ev = json.loads(ev_path.read_text())
        exp = ev.get("statistics", {}).get("trade_return_distribution", {})
        t = trades[trades["strategy"] == name] if not trades.empty else trades
        n = len(t)
        mean_ret = float(t["return_pct"].mean()) if n else np.nan
        win = float((t["net_pnl"] > 0).mean()) if n else np.nan
        exp_win = ev.get("test", {}).get("win_rate")
        flag = ""
        if n >= 10 and exp.get("p05") is not None and mean_ret < exp["p05"]:
            flag = "DEGRADED: mean trade return below research 5th percentile"
        elif n >= 10 and exp_win is not None and win < exp_win - 0.15:
            flag = "DEGRADED: win rate >15pp below research"
        rows.append({"strategy": name, "research_status": ev.get("status"), "paper_trades": n,
                     "paper_mean_trade_return": mean_ret, "research_mean_trade_return": exp.get("mean"),
                     "paper_win_rate": win, "research_test_win_rate": exp_win, "flag": flag or ("insufficient paper trades" if n < 10 else "ok")})
    return pd.DataFrame(rows)


def weekly_workflow(cfg: AppConfig, as_of=None, force: bool = False) -> dict:
    d = pd.Timestamp(as_of) if as_of else pd.Timestamp(now_ist().date())
    key = f"weekly:{d.isocalendar().year}-W{d.isocalendar().week:02d}"

    def review(ctx):
        ledger = PaperLedger(cfg.path("paper_db"))
        name = cfg.settings.paper_trading.portfolio_name
        fr = load_portfolio_frames(ledger, name)
        if fr["equity"].empty:
            raise WorkflowStopped("no paper-trading history yet")
        trades = fr["trades"]
        by_strat = (trades.groupby("strategy").agg(trades=("net_pnl", "size"), net_pnl=("net_pnl", "sum"),
                                                   win_rate=("net_pnl", lambda s: (s > 0).mean()))
                    .reset_index() if not trades.empty else pd.DataFrame())
        pos = fr["positions"]
        exposure = (pos.groupby("sector")["market_value"].sum() / fr["equity"]["equity"].iloc[-1]).rename(
            "weight").reset_index() if not pos.empty else pd.DataFrame()
        eq = fr["equity"]
        regime = eq.groupby("regime").size().rename("sessions").reset_index()
        degr = strategy_degradation(cfg)
        store = MarketStore(cfg.path("market_db"), read_only=True)
        b = store.benchmark(cfg.settings.backtest.benchmark).set_index("date")["close"]
        ctx["report"] = build_period_report(
            ledger, name, "weekly", cfg.path("reports_dir"), b, d, store.any_synthetic(),
            cfg.settings.backtest.risk_free_rate_annual,
            extra_sections=[("Strategy comparison (paper)", by_strat), ("Sector exposure", exposure),
                            ("Regime analysis (sessions per regime)", regime),
                            ("Strategy degradation monitoring", degr),
                            ("Drawdown analysis", f"Current drawdown from peak: "
                                                  f"{eq['equity'].iloc[-1] / eq['equity'].cummax().iloc[-1] - 1:.2%}; "
                                                  f"max drawdown: {(eq['equity'] / eq['equity'].cummax() - 1).min():.2%}")])

    return run_workflow(cfg, "weekly", key, [("weekly_review", review)], force)


# =========================================================================== monthly
def monthly_workflow(cfg: AppConfig, as_of=None, force: bool = False) -> dict:
    d = pd.Timestamp(as_of) if as_of else pd.Timestamp(now_ist().date())
    key = f"monthly:{d:%Y-%m}"
    root = cfg.path("reports_dir") / "research"

    def reassess(ctx):
        prev = json.loads((root / "latest.json").read_text()) if (root / "latest.json").exists() else None
        res = pipeline.run_research(cfg)
        ctx["research"] = res
        # parameter stability vs previous research run
        rows = []
        if prev:
            for name in res["statuses"]:
                p_old = root / prev["run_id"] / name / "evaluation.json"
                p_new = res["out_dir"] / name / "evaluation.json"
                if p_old.exists() and p_new.exists():
                    o, n = json.loads(p_old.read_text()), json.loads(p_new.read_text())
                    rows.append({"strategy": name, "previous_params": json.dumps(o["selected_params"]),
                                 "new_params": json.dumps(n["selected_params"]),
                                 "changed": o["selected_params"] != n["selected_params"],
                                 "previous_status": o["status"], "new_status": n["status"]})
        pd.DataFrame(rows).to_csv(res["out_dir"] / "parameter_stability_vs_previous.csv", index=False)

    def monthly_report(ctx):
        ledger = PaperLedger(cfg.path("paper_db"))
        name = cfg.settings.paper_trading.portfolio_name
        if ledger.last_session(name) is None:
            raise WorkflowStopped("no paper-trading history yet (research reassessment completed)")
        store = MarketStore(cfg.path("market_db"), read_only=True)
        b = store.benchmark(cfg.settings.backtest.benchmark).set_index("date")["close"]
        summ = pd.read_csv(ctx["research"]["out_dir"] / "summary.csv") if "research" in ctx else pd.DataFrame()
        cost = summ[["strategy", "test_cagr_gross", "test_cagr_net"]] if not summ.empty else summ
        ctx["report"] = build_period_report(
            ledger, name, "monthly", cfg.path("reports_dir"), b, d, store.any_synthetic(),
            cfg.settings.backtest.risk_free_rate_annual,
            extra_sections=[("Strategy reassessment", summ), ("Transaction-cost sensitivity (test gross vs net)", cost),
                            ("Portfolio diversification (WF OOS)", json.dumps(ctx.get("research", {}).get("portfolio", {}),
                                                                              default=str)[:4000])])

    return run_workflow(cfg, "monthly", key, [("strategy_reassessment", reassess), ("monthly_report", monthly_report)],
                        force)


def daemon(cfg: AppConfig, poll_seconds: int = 300) -> None:  # pragma: no cover - long-running loop
    """Simple long-running scheduler (alternative to cron). Runs daily after the configured IST time,
    weekly on Saturday, monthly on the first Saturday of the month."""
    hh, mm = map(int, cfg.settings.scheduler.daily_run_after_ist.split(":"))
    while True:
        now = now_ist()
        try:
            if (now.hour, now.minute) >= (hh, mm):
                daily_workflow(cfg)
                if now.weekday() == 5:
                    weekly_workflow(cfg)
                    if now.day <= 7:
                        monthly_workflow(cfg)
        except WorkflowLocked as e:
            log_event(log, str(e), level=30)
        time.sleep(poll_seconds)
