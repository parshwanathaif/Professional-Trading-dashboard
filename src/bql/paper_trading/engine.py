"""Automated PAPER-TRADING engine.

Uses exactly the same PortfolioSimulator as the backtester, persisted session by session in the
SQLite ledger. There is NO broker connectivity: every order lives and dies inside this process.

Safety rules
------------
* Data-quality gate must pass; excluded symbols are removed; a freshness check verifies the latest
  session is complete (benchmark present, symbol coverage vs previous session). Otherwise: STOP.
* Only strategies whose latest research status is allowed (config: paper_trading.allowed_statuses)
  may open positions. `test_mode_allow_unvalidated` exists for SOFTWARE TESTING only; every
  decision taken under it is tagged test_mode=1.
* SHADOW book (owner opt-in, `paper_trading.shadow`): a separate portfolio that follows the top
  research-score strategy regardless of validation status, to build a live out-of-sample record.
  Every shadow decision is tagged test_mode=1. Validation gates are NOT changed by it.
* Strategies use the parameters locked by the latest research run (selected on training data).
* Kill switch and strategy suspension are ENFORCED and persist until a manual reset.
* Each session is committed atomically and only once (idempotent; safe to re-run / restart).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict

import numpy as np
import pandas as pd

from .. import PAPER_ONLY_NOTICE
from ..backtesting.engine import ResearchContext
from ..backtesting.simulator import PortfolioSimulator, SimConfig
from ..config import AppConfig
from ..data import MarketStore, load_panel_from_store
from ..data.validation import validate_store
from ..logging_setup import get_logger, log_event
from ..risk import RiskManager, RiskState
from ..strategies import make_strategy
from ..validation.research import latest_statuses
from .ledger import PaperLedger

log = get_logger("paper")


class PaperTradingHalted(RuntimeError):
    pass


def select_best_strategies(research_root, sel) -> list[str]:
    """Strategies chosen by out-of-sample evidence (research score), never by raw past return.

    Only used when `strategy_selection.mode == 'best_evidence'`. A strategy must have at least
    `min_status`; Rejected strategies are never selected.
    """
    import json
    from pathlib import Path

    from ..validation.research import STATUSES

    if sel.mode != "best_evidence":
        return []
    p = Path(research_root) / "latest.json"
    if not p.exists():
        return []
    run_dir = Path(research_root) / json.loads(p.read_text())["run_id"]
    summ = run_dir / "summary.csv"
    if not summ.exists():
        return []
    df = pd.read_csv(summ)
    floor = STATUSES.index(sel.min_status)
    df = df[df["status"].map(lambda s: STATUSES.index(s) if s in STATUSES else -1) >= floor]
    df = df[df["status"] != "Rejected"].sort_values("score", ascending=False)
    return list(df["strategy"].head(sel.max_strategies))


def _latest_run_dir(research_root):
    import json
    from pathlib import Path

    p = Path(research_root) / "latest.json"
    if not p.exists():
        return None
    return Path(research_root) / json.loads(p.read_text())["run_id"]


def research_parameters(research_root) -> dict[str, dict]:
    """Parameters locked by the latest research run (chosen on TRAINING data only)."""
    import json

    run_dir = _latest_run_dir(research_root)
    out = {}
    if run_dir is None:
        return out
    for ev in run_dir.glob("*/evaluation.json"):
        e = json.loads(ev.read_text())
        out[e["strategy"]] = e.get("selected_params") or {}
    return out


def select_shadow_strategies(research_root, shadow_cfg) -> list[str]:
    """SHADOW book (owner opt-in): the top research-score strategies REGARDLESS of status, excluding any
    that failed the look-ahead leakage test. Decisions are tagged unvalidated (test_mode=1)."""
    import json

    run_dir = _latest_run_dir(research_root)
    if run_dir is None or not (run_dir / "summary.csv").exists():
        return []
    df = pd.read_csv(run_dir / "summary.csv")
    ok = []
    for name in df.sort_values("score", ascending=False)["strategy"]:
        ev = json.loads((run_dir / name / "evaluation.json").read_text())
        if ev.get("leakage", {}).get("passed"):
            ok.append(name)
    return ok[: shadow_cfg.max_strategies]


def freshness_check(panel, t: int, min_coverage: float = 0.9) -> tuple[bool, str]:
    if t <= 0:
        return True, "first session"
    if not np.isfinite(panel.benchmark.iloc[t]):
        return False, f"benchmark missing on {panel.dates[t].date()}"
    prev = panel.available[t - 1]
    if prev.sum() == 0:
        return True, "no previous coverage"
    cov = (panel.available[t] & prev).sum() / prev.sum()
    if cov < min_coverage:
        return False, f"only {cov:.0%} of symbols with data on {panel.dates[t - 1].date()} have data on {panel.dates[t].date()}"
    return True, f"coverage {cov:.0%}"


class PaperTrader:
    def __init__(self, cfg: AppConfig, portfolio: str | None = None, universe_index: str | None = None,
                 shadow: bool = False):
        self.cfg = cfg
        self.pcfg = cfg.settings.paper_trading
        self.shadow = shadow
        if shadow and not self.pcfg.shadow.enabled:
            raise ValueError("shadow portfolio disabled in config (paper_trading.shadow.enabled)")
        self.name = portfolio or (self.pcfg.shadow.portfolio_name if shadow else self.pcfg.portfolio_name)
        self.store = MarketStore(cfg.path("market_db"), read_only=True)
        self.ledger = PaperLedger(cfg.path("paper_db"))
        self.universe_index = universe_index

    def _strategy_permissions(self) -> tuple[dict[str, bool], dict[str, str]]:
        root = self.cfg.path("reports_dir") / "research"
        statuses = latest_statuses(root)
        if self.shadow:
            chosen = select_shadow_strategies(root, self.pcfg.shadow)
            return {n: (n in chosen) for n, e in self.cfg.strategies.strategies.items() if e.enabled}, statuses
        allowed = {}
        for name, entry in self.cfg.strategies.strategies.items():
            if not entry.enabled:
                continue
            ok = statuses.get(name) in self.pcfg.allowed_statuses
            allowed[name] = ok or self.pcfg.test_mode_allow_unvalidated
        for name in select_best_strategies(root, self.pcfg.strategy_selection):
            if name in allowed:
                allowed[name] = True
        return allowed, statuses

    def run(self, as_of=None, start=None, max_sessions: int | None = None) -> dict:
        cfg = self.cfg
        run_id = uuid.uuid4().hex[:12]
        dq = validate_store(self.store, cfg, as_of=as_of)
        dq.save(cfg.path("reports_dir") / "data_quality", tag="paper")
        if dq.blocked:
            log_event(log, "paper run blocked by data quality", level=40, summary=dq.summary)
            raise PaperTradingHalted(f"data-quality gate failed: {dq.summary}")

        panel = load_panel_from_store(self.store, cfg, exclude_symbols=dq.excluded_symbols,
                                      universe_index=self.universe_index, exclude_rows=dq.excluded_rows)
        if as_of is not None:
            panel = panel.truncate(as_of)
        ctx = ResearchContext.build(cfg, panel)
        allowed, statuses = self._strategy_permissions()
        # shadow decisions are unvalidated by definition -> tagged test_mode=1 in the audit trail
        test_mode = self.pcfg.test_mode_allow_unvalidated or self.shadow
        locked = research_parameters(cfg.path("reports_dir") / "research")
        signals = {}
        for name, entry in cfg.strategies.strategies.items():
            if entry.enabled:
                params = {**entry.params, **locked.get(name, {})}   # research-locked params win
                strat = make_strategy(name, params, cfg.risk.position.atr_stop_multiple)
                signals[name] = strat.generate(panel, ctx.universe)

        last_date, state = self.ledger.load_snapshot(self.name)
        if last_date is None:
            first = panel.dates[-1] if start is None else panel.dates[panel.dates >= pd.Timestamp(start)][0]
            t_start = panel.index_of(first)
        else:
            later = np.flatnonzero(panel.dates > pd.Timestamp(last_date))
            if later.size == 0:
                return {"status": "UP_TO_DATE", "last_session": last_date, "run_id": run_id}
            t_start = int(later[0])
        t_end = panel.T - 1 if max_sessions is None else min(panel.T - 1, t_start + max_sessions - 1)

        self.ledger.ensure_portfolio(self.name, self.pcfg.initial_capital_inr, cfg.config_hash(), test_mode,
                                     PAPER_ONLY_NOTICE)
        bt = cfg.settings.backtest
        sim_cfg = SimConfig(initial_capital=self.pcfg.initial_capital_inr, delay=bt.execution_delay_bars,
                            fill_price=bt.fill_price, participation=bt.max_participation_of_volume,
                            delisting_haircut=bt.delisting_haircut, stale_days=bt.stale_price_mark_days,
                            record_signals=True, gap_alert=cfg.risk.gap_risk.overnight_gap_alert, test_mode=test_mode)
        risk = RiskManager(cfg.risk, RiskState.from_dict(state["risk"]) if state else None, enforce_hard_limits=True)
        gating = {k: v.get("block", []) for k, v in cfg.strategies.regime_gating.items()}
        sim = PortfolioSimulator(panel, signals, ctx.cost, risk, sim_cfg, regimes=ctx.regimes["regime"],
                                 universe=ctx.universe, regime_gating=gating,
                                 max_holding_days=cfg.risk.position.max_holding_days, strategy_allowed=allowed)
        if state:
            sim.load_state(state)
        processed = []
        for t in range(t_start, t_end + 1):
            ok, why = freshness_check(panel, t)
            if not ok:
                log_event(log, "paper run stopped: incomplete data", level=40, reason=why)
                raise PaperTradingHalted(f"incomplete data: {why}")
            n0 = {k: len(getattr(sim, k)) for k in ("signal_log", "orders_log", "fills", "trades", "alerts")}
            ev0 = len(risk.state.events)
            sim.step(t, decide=True)
            ds = str(panel.dates[t].date())
            positions = [{"symbol": p.symbol, "strategy": p.strategy, "qty": p.qty, "entry_date": p.entry_date,
                          "entry_price": p.entry_fill_price, "last_price": p.last_price,
                          "market_value": p.qty * p.last_price,
                          "unrealised_pnl": p.qty * p.last_price - p.cost_basis_total * p.qty / p.orig_qty,
                          "stop": p.stop, "sector": p.sector} for p in sim.positions.values()]
            eq_row = {**sim.equity_rows[-1]}
            alerts = sim.alerts[n0["alerts"]:] + [e for e in risk.state.events[ev0:] if e not in sim.alerts[n0["alerts"]:]]
            fp = hashlib.sha256(f"{ds}|{panel.N}|{np.nansum(panel.close[t]):.6f}".encode()).hexdigest()[:16]
            self.ledger.commit_session(
                self.name, ds, run_id, cfg.config_hash(), fp, sim.state_dict(),
                sim.signal_log[n0["signal_log"]:], sim.orders_log[n0["orders_log"]:], sim.fills[n0["fills"]:],
                sim.trades[n0["trades"]:], eq_row, positions, alerts)
            processed.append(ds)
        summary = {"status": "OK", "run_id": run_id, "sessions_processed": processed,
                   "equity": sim.equity_rows[-1]["equity"] if sim.equity_rows else None,
                   "open_positions": len(sim.positions), "pending_orders": len(sim.pending),
                   "kill_switch": risk.state.kill_switch, "suspended": dict(risk.state.suspended),
                   "strategy_permissions": allowed, "research_statuses": statuses, "test_mode": test_mode,
                   "portfolio": self.name, "shadow": self.shadow,
                   "strategy_params": {k: {**cfg.strategies.strategies[k].params, **locked.get(k, {})}
                                       for k, v in allowed.items() if v},
                   "excluded_symbols": dq.excluded_symbols}
        log_event(log, "paper run complete", **{k: v for k, v in summary.items() if k != "sessions_processed"},
                  n_sessions=len(processed))
        return summary

    # ------------------------------------------------------------------ manual controls
    def reset_kill_switch(self, operator: str, note: str) -> dict:
        _, state = self.ledger.load_snapshot(self.name)
        if state is None:
            raise RuntimeError("no paper portfolio state found")
        rm = RiskManager(self.cfg.risk, RiskState.from_dict(state["risk"]))
        if not rm.state.kill_switch:
            return {"status": "NOT_ACTIVE"}
        ev = rm.reset_kill_switch(operator, note)
        self.ledger.update_risk_state(self.name, rm.state.to_dict(), asdict(ev))
        return {"status": "RESET", "event": asdict(ev)}

    def reset_strategy(self, strategy: str, operator: str, note: str) -> dict:
        _, state = self.ledger.load_snapshot(self.name)
        if state is None:
            raise RuntimeError("no paper portfolio state found")
        rm = RiskManager(self.cfg.risk, RiskState.from_dict(state["risk"]))
        ev = rm.reset_strategy(strategy, operator, note)
        self.ledger.update_risk_state(self.name, rm.state.to_dict(), asdict(ev))
        return {"status": "RESET", "event": asdict(ev)}
