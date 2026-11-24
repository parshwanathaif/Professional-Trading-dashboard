"""Backtest runner built on the shared PortfolioSimulator."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..config import AppConfig
from ..costs import CostModel
from ..data.panel import Panel
from ..regime import detect_from_cfg
from ..risk import RiskManager
from ..strategies import Signals, make_strategy
from .metrics import compute_metrics
from .simulator import PortfolioSimulator, SimConfig


def config_universe(panel: Panel, cfg: AppConfig) -> np.ndarray:
    """Universe mask exactly as configured (single source of truth for research, leakage tests and paper)."""
    d = cfg.settings.data
    top_n = d.universe_top_n if d.universe_mode == "liquidity_top_n" else None
    return panel.universe_mask(d.min_median_traded_value_inr, d.min_price_inr, d.min_history_days,
                               top_n=top_n, rank_window=d.universe_rank_window)


@dataclass
class ResearchContext:
    """Everything a backtest needs that does not depend on strategy parameters."""

    cfg: AppConfig
    panel: Panel
    universe: np.ndarray
    regimes: pd.DataFrame
    cost: CostModel
    data_flags: dict = field(default_factory=dict)

    @classmethod
    def build(cls, cfg: AppConfig, panel: Panel, data_flags: dict | None = None) -> ResearchContext:
        uni = config_universe(panel, cfg)
        reg = detect_from_cfg(panel.benchmark, cfg.strategies.regime)
        d = cfg.settings.data
        flags = {"survivorship_safe": panel.survivorship_safe, "synthetic": panel.synthetic,
                 "point_in_time_universe": panel.members is not None or (
                     d.universe_mode == "liquidity_top_n" and bool(panel.meta.get("complete_exchange_history"))),
                 "universe_rule": (f"index membership {panel.meta.get('universe_index')}" if panel.members is not None
                                   else f"top {d.universe_top_n} by trailing {d.universe_rank_window}d median traded value"
                                   if d.universe_mode == "liquidity_top_n" else "all liquid securities"),
                 **(data_flags or {})}
        return cls(cfg, panel, uni, reg, CostModel(cfg.costs), flags)


@dataclass
class BacktestResult:
    strategy: str
    params: dict
    equity: pd.DataFrame
    trades: pd.DataFrame
    fills: pd.DataFrame
    orders: pd.DataFrame
    signals: pd.DataFrame
    alerts: pd.DataFrame
    metrics: dict
    run_meta: dict

    @property
    def returns(self) -> pd.Series:
        return self.equity.set_index("date")["equity"].pct_change().fillna(0.0)


def run_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


def run_backtest(ctx: ResearchContext, strategies: dict[str, dict] | str, params: dict | None = None,
                 start=None, end=None, cost: CostModel | None = None, delay: int | None = None,
                 participation: float | None = None, record_signals: bool = True,
                 signals: dict[str, Signals] | None = None, extra_slippage_bps: float = 0.0,
                 panel_override: Panel | None = None, apply_regime_gating: bool = True,
                 enforce_hard_limits: bool = False) -> BacktestResult:
    """Run a backtest over [start, end].

    Signals are computed on the full panel (all indicators are causal; verified by the leakage
    test) and the simulation only acts on sessions inside the window, so warm-up history before
    `start` is used for indicators but no trades occur before `start`.
    """
    cfg = ctx.cfg
    panel = panel_override or ctx.panel
    if isinstance(strategies, str):
        strategies = {strategies: params or {}}
    if signals is None:
        signals = {}
        for name, prm in strategies.items():
            base = cfg.strategies.strategies[name].params if name in cfg.strategies.strategies else {}
            strat = make_strategy(name, {**base, **(prm or {})}, atr_mult=cfg.risk.position.atr_stop_multiple)
            signals[name] = strat.generate(panel, ctx.universe)
    bt = cfg.settings.backtest
    sim = SimConfig(
        initial_capital=bt.initial_capital_inr, delay=delay or bt.execution_delay_bars, fill_price=bt.fill_price,
        participation=participation or bt.max_participation_of_volume, delisting_haircut=bt.delisting_haircut,
        stale_days=bt.stale_price_mark_days, record_signals=record_signals, extra_slippage_bps=extra_slippage_bps,
        gap_alert=cfg.risk.gap_risk.overnight_gap_alert,
    )
    gating = {k: v.get("block", []) for k, v in cfg.strategies.regime_gating.items()} if apply_regime_gating else {}
    risk = RiskManager(cfg.risk, enforce_hard_limits=enforce_hard_limits)
    simulator = PortfolioSimulator(panel, signals, cost or ctx.cost, risk, sim,
                                   regimes=ctx.regimes["regime"], universe=ctx.universe, regime_gating=gating,
                                   max_holding_days=cfg.risk.position.max_holding_days)
    dates = panel.dates
    t0 = 0 if start is None else int(np.searchsorted(dates.values, np.datetime64(pd.Timestamp(start))))
    t1 = len(dates) - 1 if end is None else int(np.searchsorted(dates.values, np.datetime64(pd.Timestamp(end)),
                                                                side="right")) - 1
    for t in range(t0, t1 + 1):
        simulator.step(t, decide=t < t1)
    eq = pd.DataFrame(simulator.equity_rows)
    trades = pd.DataFrame(simulator.trades)
    metrics = compute_metrics(eq, trades, panel.benchmark, bt.risk_free_rate_annual, simulator.turnover)
    meta = {
        "config_hash": cfg.config_hash(), "strategies": strategies, "start": str(dates[t0].date()),
        "end": str(dates[t1].date()), "delay": sim.delay, "participation": sim.participation,
        "cost_multiplier": (cost or ctx.cost).cost_multiplier,
        "slippage_multiplier": (cost or ctx.cost).slippage_multiplier, "data_flags": ctx.data_flags,
        "open_positions_at_end": len(simulator.positions), "unfilled_orders_at_end": len(simulator.pending),
        "hard_limits_enforced": enforce_hard_limits,
        "hard_limit_events_logged": risk.would_have_triggered,
    }
    meta["run_hash"] = run_hash({k: v for k, v in meta.items()})
    name = "+".join(strategies)
    return BacktestResult(
        strategy=name, params=strategies, equity=eq, trades=trades, fills=pd.DataFrame(simulator.fills),
        orders=pd.DataFrame(simulator.orders_log), signals=pd.DataFrame(simulator.signal_log),
        alerts=pd.DataFrame(simulator.alerts), metrics=metrics, run_meta=meta,
    )
