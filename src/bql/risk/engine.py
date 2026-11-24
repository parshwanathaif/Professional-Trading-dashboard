"""Independent risk-management engine.

The risk engine is the only component allowed to size positions and to permit new entries.
Strategies have no access to it and cannot override it. It is used identically by the
backtester and the paper-trading engine.

Controls
--------
* Per-position: capital-at-risk sizing to an ATR stop, max weight, liquidity (ADV) cap,
  minimum ticket size.
* Portfolio: max open positions, gross-exposure cap (no leverage), sector cap, correlation
  limit, volatility targeting, cash buffer.
* Loss limits: daily and weekly loss (soft: entries blocked, auto-clears), drawdown warning,
  drawdown KILL SWITCH (hard: entries blocked until explicit manual reset).
* Strategy suspension after N consecutive losing trades (manual reset).
* Market-stress regime gating and regime-dependent exposure caps.
* Gap-risk alerts.
Martingale, averaging down and pyramiding are structurally impossible: one position per
symbol, no adds to existing positions, size derived only from equity and stop distance.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from ..config import RiskLimits


@dataclass
class RiskEvent:
    date: str
    severity: str       # INFO | WARNING | CRITICAL
    code: str
    detail: str
    strategy: str | None = None


@dataclass
class RiskState:
    kill_switch: bool = False
    kill_reason: str | None = None
    kill_date: str | None = None
    entry_block_until: str | None = None          # soft block (daily/weekly loss)
    entry_block_reason: str | None = None
    suspended: dict[str, str] = field(default_factory=dict)
    consecutive_losses: dict[str, int] = field(default_factory=dict)
    peak_equity: float = 0.0
    week_start_equity: float | None = None
    week_id: str | None = None
    last_equity: float | None = None
    events: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> RiskState:
        return cls(**d)


@dataclass
class SizingDecision:
    qty: int
    binding: str
    detail: dict
    approved: bool
    reason: str = ""


class RiskManager:
    def __init__(self, limits: RiskLimits, state: RiskState | None = None, enforce_hard_limits: bool = True):
        """`enforce_hard_limits=False` is research mode: kill-switch and strategy-suspension events are
        recorded (so their frequency can be studied) but do not halt the simulation, because a
        historical simulation cannot model the required manual review/reset. Paper trading always
        enforces them."""
        self.L = limits
        self.state = state or RiskState()
        self.enforce = enforce_hard_limits
        self.would_have_triggered: list[dict] = []

    # ------------------------------------------------------------------ events
    def _event(self, date, severity, code, detail, strategy=None) -> RiskEvent:
        ev = RiskEvent(str(pd.Timestamp(date).date()), severity, code, detail, strategy)
        self.state.events.append(asdict(ev))
        if len(self.state.events) > 500:       # full history lives in the ledger / backtest output
            self.state.events = self.state.events[-500:]
        return ev

    # ------------------------------------------------------------------ after close
    def update_after_close(self, date, equity: float, regime: str | None = None) -> list[RiskEvent]:
        s, LL = self.state, self.L.loss_limits
        out: list[RiskEvent] = []
        d = pd.Timestamp(date)
        if s.peak_equity <= 0:
            s.peak_equity = equity
        s.peak_equity = max(s.peak_equity, equity)
        dd = equity / s.peak_equity - 1.0

        wid = f"{d.isocalendar().year}-W{d.isocalendar().week:02d}"
        if s.week_id != wid:
            s.week_id = wid
            s.week_start_equity = s.last_equity if s.last_equity else equity
        if s.last_equity:
            day_ret = equity / s.last_equity - 1.0
            if day_ret <= -LL.max_daily_loss:
                s.entry_block_until = str((d + pd.Timedelta(days=1)).date())
                s.entry_block_reason = f"daily loss {day_ret:.2%} breached limit {-LL.max_daily_loss:.2%}"
                out.append(self._event(d, "WARNING", "DAILY_LOSS_LIMIT", s.entry_block_reason))
        if s.week_start_equity:
            wk = equity / s.week_start_equity - 1.0
            if wk <= -LL.max_weekly_loss:
                # block until the end of the ISO week (Sunday)
                end_week = d + pd.Timedelta(days=6 - d.weekday())
                if not s.entry_block_until or pd.Timestamp(s.entry_block_until) < end_week:
                    s.entry_block_until = str(end_week.date())
                    s.entry_block_reason = f"weekly loss {wk:.2%} breached limit {-LL.max_weekly_loss:.2%}"
                    out.append(self._event(d, "WARNING", "WEEKLY_LOSS_LIMIT", s.entry_block_reason))
        if dd <= -LL.max_drawdown_kill and not s.kill_switch:
            reason = f"portfolio drawdown {dd:.2%} breached hard limit {-LL.max_drawdown_kill:.2%}"
            if self.enforce:
                s.kill_switch = True
                s.kill_reason = reason
                s.kill_date = str(d.date())
                out.append(self._event(d, "CRITICAL", "KILL_SWITCH", reason))
            elif not any(w["code"] == "KILL_SWITCH" for w in self.would_have_triggered):
                self.would_have_triggered.append({"date": str(d.date()), "code": "KILL_SWITCH", "detail": reason})
                out.append(self._event(d, "CRITICAL", "KILL_SWITCH_WOULD_TRIGGER", reason + " (research mode: logged)"))
        elif dd <= -LL.max_drawdown_warning:
            out.append(self._event(d, "WARNING", "DRAWDOWN_WARNING", f"drawdown {dd:.2%}"))
        s.last_equity = equity
        return out

    def record_trade_result(self, date, strategy: str, net_pnl: float) -> list[RiskEvent]:
        s = self.state
        if net_pnl < 0:
            s.consecutive_losses[strategy] = s.consecutive_losses.get(strategy, 0) + 1
        else:
            s.consecutive_losses[strategy] = 0
        if (s.consecutive_losses[strategy] >= self.L.loss_limits.max_consecutive_losses
                and strategy not in s.suspended):
            reason = f"{s.consecutive_losses[strategy]} consecutive losing trades"
            if self.enforce:
                s.suspended[strategy] = reason
                return [self._event(date, "CRITICAL", "STRATEGY_SUSPENDED", reason, strategy)]
            s.consecutive_losses[strategy] = 0
            self.would_have_triggered.append({"date": str(pd.Timestamp(date).date()), "code": "STRATEGY_SUSPENDED",
                                              "detail": reason, "strategy": strategy})
            return [self._event(date, "WARNING", "SUSPENSION_WOULD_TRIGGER", reason + " (research mode: logged)",
                                strategy)]
        return []

    # ------------------------------------------------------------------ permissions
    def entries_allowed(self, date, strategy: str, regime: str | None,
                        blocked_regimes: list[str] | None = None) -> tuple[bool, str]:
        s = self.state
        if s.kill_switch:
            return False, f"KILL SWITCH active since {s.kill_date}: {s.kill_reason}"
        if strategy in s.suspended:
            return False, f"strategy suspended: {s.suspended[strategy]}"
        if s.entry_block_until and pd.Timestamp(date) <= pd.Timestamp(s.entry_block_until):
            return False, f"entries blocked until {s.entry_block_until}: {s.entry_block_reason}"
        if regime and regime in self.L.market_stress.block_entries_in_regimes:
            return False, f"market regime {regime} blocks new entries"
        if regime and blocked_regimes and regime in blocked_regimes:
            return False, f"strategy regime gate: {regime}"
        return True, "ok"

    def exposure_cap(self, regime: str | None, realised_vol_ann: float | None) -> float:
        cap = self.L.portfolio.max_gross_exposure
        if regime and regime in self.L.market_stress.exposure_cap_in_regimes:
            cap = min(cap, self.L.market_stress.exposure_cap_in_regimes[regime])
        tgt = self.L.portfolio.volatility_target_annual
        if realised_vol_ann and np.isfinite(realised_vol_ann) and realised_vol_ann > tgt > 0:
            cap = min(cap, self.L.portfolio.max_gross_exposure * tgt / realised_vol_ann)
        return max(0.0, min(cap, 1.0))

    def correlation_ok(self, cand_returns: np.ndarray, held_returns: np.ndarray) -> tuple[bool, float]:
        """Average correlation of the candidate with current holdings over the lookback window."""
        if held_returns is None or held_returns.size == 0 or held_returns.shape[1] == 0:
            return True, float("nan")
        M = np.column_stack([cand_returns, held_returns])
        M = M[np.isfinite(M).all(axis=1)]
        if M.shape[0] < 30:
            return True, float("nan")
        sd = M.std(axis=0)
        keep = np.flatnonzero(sd[1:] > 0) + 1
        if sd[0] == 0 or keep.size == 0:
            return True, float("nan")
        Z = (M - M.mean(axis=0)) / np.where(sd > 0, sd, 1.0)
        corrs = (Z[:, keep].T @ Z[:, 0]) / M.shape[0]
        avg = float(np.mean(corrs))
        return avg <= self.L.portfolio.max_avg_pairwise_correlation, avg

    # ------------------------------------------------------------------ sizing
    def size_position(self, *, equity: float, cash_available: float, invested_value: float,
                      sector_value: float, n_open: int, price: float, stop_distance: float,
                      adv_value: float, exposure_cap: float, est_cost_frac: float = 0.003) -> SizingDecision:
        P, PL, LQ = self.L.position, self.L.portfolio, self.L.liquidity
        if n_open >= P.max_open_positions:
            return SizingDecision(0, "max_open_positions", {}, False, f"max open positions {P.max_open_positions}")
        if not (price > 0) or not np.isfinite(price):
            return SizingDecision(0, "invalid_price", {}, False, "invalid price")
        if not (stop_distance > 0) or not np.isfinite(stop_distance):
            return SizingDecision(0, "invalid_stop", {}, False, "stop distance unavailable")
        caps = {
            "risk_budget": equity * P.risk_per_position / stop_distance,
            "max_position_weight": equity * P.max_position_weight / price,
            "liquidity_adv": (LQ.max_adv_participation_at_sizing * adv_value / price) if adv_value > 0 else 0.0,
            "exposure_cap": max(0.0, exposure_cap * equity - invested_value) / price,
            "sector_cap": max(0.0, PL.max_sector_exposure * equity - sector_value) / price,
            "cash": max(0.0, cash_available - PL.min_cash_buffer * equity) / (price * (1 + est_cost_frac)),
        }
        binding = min(caps, key=caps.get)
        qty = int(math.floor(max(0.0, caps[binding])))
        detail = {k: round(v, 2) for k, v in caps.items()}
        if qty * price < P.min_position_value_inr:
            return SizingDecision(0, binding, detail, False,
                                  f"position below minimum ticket after '{binding}' constraint")
        return SizingDecision(qty, binding, detail, True, "approved")

    # ------------------------------------------------------------------ manual reset
    def reset_kill_switch(self, operator: str, note: str, date=None) -> RiskEvent:
        if not operator or not note:
            raise ValueError("manual reset requires operator name and a note")
        prev = self.state.kill_reason
        self.state.kill_switch = False
        self.state.kill_reason = None
        self.state.kill_date = None
        # reset the drawdown reference so the switch does not immediately re-trigger
        if self.state.last_equity:
            self.state.peak_equity = self.state.last_equity
        return self._event(date or pd.Timestamp.today(), "INFO", "KILL_SWITCH_RESET",
                           f"reset by {operator}: {note} (previous: {prev})")

    def reset_strategy(self, strategy: str, operator: str, note: str, date=None) -> RiskEvent:
        if not operator or not note:
            raise ValueError("manual reset requires operator name and a note")
        self.state.suspended.pop(strategy, None)
        self.state.consecutive_losses[strategy] = 0
        return self._event(date or pd.Timestamp.today(), "INFO", "STRATEGY_RESET",
                           f"{strategy} reset by {operator}: {note}", strategy)
