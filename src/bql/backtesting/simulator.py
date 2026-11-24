"""Event-driven daily portfolio simulator (shared by backtester and paper-trading engine).

Daily event sequence for session t
----------------------------------
1. EXECUTION (open of t): pending orders whose execution session is t are filled
   (sells first, then buys) at the session's open (or close if configured), subject to:
   bar availability (halts / missing data -> no fill), circuit locks (no buys into an
   upper-circuit lock, no sells into a lower-circuit lock), volume participation cap,
   spread + slippage + square-root impact, statutory charges, cash availability.
2. STOPS (intraday t): protective stops set at entry are checked against the bar's low;
   gap-downs fill at the open, otherwise at the stop level (minus slippage).
3. CORPORATE ACTIONS: cash dividends credited on the ex-date to positions held at the
   previous close. Splits/bonus/rights are reflected via adjusted prices.
4. STALE / DELISTED: positions without a price for more than `stale_price_mark_days`
   sessions are written off at last price x (1 - delisting_haircut).
5. MARK-TO-MARKET at close t (missing bar -> last price, counted as stale).
6. RISK UPDATE after close (loss limits, drawdown kill switch).
7. DECISIONS at close t: strategy exits, time stops, then new entries ranked by score,
   each passed through the independent risk engine. Orders execute at session
   t + execution_delay_bars (>= 1, never the same bar).
"""

from __future__ import annotations

import math
import uuid
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from ..costs import CostModel
from ..data.panel import Panel
from ..risk import RiskManager
from ..strategies.base import Signals


@dataclass
class Position:
    symbol: str
    strategy: str
    qty: int
    orig_qty: int
    entry_date: str
    entry_ref_price: float
    entry_fill_price: float
    stop: float
    cost_basis_total: float          # qty*fill + buy charges
    entry_charges: float
    entry_exec_cost: float
    signal_date: str
    entry_reason: str
    entry_info: dict
    sector: str
    last_price: float
    stale_days: int = 0
    bars_held: int = 0
    dividends: float = 0.0
    realized_net: float = 0.0
    realized_gross: float = 0.0
    exit_charges: float = 0.0
    exit_exec_cost: float = 0.0
    exit_value: float = 0.0


@dataclass
class Order:
    order_id: str
    signal_date: str
    symbol: str
    strategy: str
    side: str
    qty: int
    reason: str
    stop_distance: float = float("nan")
    score: float = float("nan")
    info: dict = field(default_factory=dict)
    attempts: int = 0
    test_mode: bool = False


@dataclass
class SimConfig:
    initial_capital: float
    delay: int = 1
    fill_price: str = "open"
    participation: float = 0.05
    delisting_haircut: float = 0.30
    stale_days: int = 5
    record_signals: bool = True
    extra_slippage_bps: float = 0.0
    gap_alert: float = 0.08
    test_mode: bool = False


class PortfolioSimulator:
    def __init__(self, panel: Panel, signals: dict[str, Signals], cost: CostModel, risk: RiskManager,
                 sim: SimConfig, regimes: pd.Series | None = None, universe: np.ndarray | None = None,
                 regime_gating: dict[str, list[str]] | None = None, max_holding_days: int = 250,
                 strategy_allowed: dict[str, bool] | None = None):
        self.p = panel
        self.signals = signals
        self.cost = cost
        self.risk = risk
        self.sim = sim
        self.regimes = regimes.reindex(panel.dates).fillna("UNKNOWN") if regimes is not None else \
            pd.Series("UNKNOWN", index=panel.dates)
        self.universe = universe if universe is not None else panel.available
        self.gating = regime_gating or {}
        self.max_holding_days = max_holding_days
        self.strategy_allowed = strategy_allowed or {}
        self.col = {s: j for j, s in enumerate(panel.symbols)}
        self.date_idx = {str(d.date()): i for i, d in enumerate(panel.dates)}
        # causal helpers
        val = pd.DataFrame(panel.value)
        self.adv = val.rolling(20, min_periods=10).median().to_numpy()
        r = panel.returns()
        self.rets = r
        self.dvol = pd.DataFrame(r).rolling(20, min_periods=10).std().to_numpy()
        # state
        self.cash = float(sim.initial_capital)
        self.positions: dict[str, Position] = {}
        self.pending: list[Order] = []
        self.trades: list[dict] = []
        self.fills: list[dict] = []
        self.orders_log: list[dict] = []
        self.signal_log: list[dict] = []
        self.equity_rows: list[dict] = []
        self.alerts: list[dict] = []
        self.cum_charges = 0.0
        self.cum_exec_cost = 0.0
        self.cum_dividends = 0.0
        self.turnover = 0.0

    # =================================================================== helpers
    def _date(self, t: int) -> str:
        return str(self.p.dates[t].date())

    def _exec_index(self, o: Order) -> int:
        k = self.date_idx.get(o.signal_date)
        return k + self.sim.delay if k is not None else -1

    def _locked(self, t: int, j: int) -> tuple[bool, bool]:
        """(upper_locked, lower_locked) for bar t of symbol j."""
        p = self.p
        if t == 0 or not p.available[t, j]:
            return False, False
        h, lo, c = p.high[t, j], p.low[t, j], p.close[t, j]
        prev = p.close[t - 1, j]
        if not np.isfinite(prev) or prev <= 0:
            return False, False
        locked = abs(h - lo) <= 1e-9 * max(h, 1.0)
        if not locked:
            return False, False
        move = c / prev - 1.0
        band = p.band_pct[t, j] if p.band_pct is not None else np.nan
        thresh = (band - 0.0015) if np.isfinite(band) else 0.019
        return move >= thresh, move <= -thresh

    def _equity(self) -> float:
        return self.cash + sum(pos.qty * pos.last_price for pos in self.positions.values())

    def _invested(self) -> float:
        return sum(pos.qty * pos.last_price for pos in self.positions.values())

    def _record_fill(self, t, o: Order | None, symbol, side, qty, ref, fill, charges, exec_cost, reason):
        self.fills.append({
            "fill_id": uuid.uuid4().hex[:12], "date": self._date(t), "symbol": symbol, "side": side, "qty": qty,
            "ref_price": round(ref, 4), "fill_price": round(fill, 4), "value": round(qty * fill, 2),
            "charges": round(charges.total, 2), "charges_detail": charges.as_dict(), "exec_cost_inr": round(exec_cost, 2),
            "order_id": o.order_id if o else None, "strategy": o.strategy if o else None, "reason": reason,
            "signal_date": o.signal_date if o else None, "test_mode": self.sim.test_mode,
        })
        self.cum_charges += charges.total
        self.cum_exec_cost += exec_cost
        self.turnover += qty * fill

    def _sell(self, t: int, pos: Position, qty: int, ref: float, reason: str, o: Order | None = None,
              haircut: float | None = None) -> None:
        j = self.col[pos.symbol]
        date = self.p.dates[t]
        if haircut is not None:
            fill, frac = ref * (1 - haircut), 0.0
            exec_cost = 0.0
        else:
            vol = self.p.volume[t, j] if np.isfinite(self.p.volume[t, j]) else 0.0
            fill, frac = self.cost.execution_price("SELL", ref, qty, vol, self.adv[t - 1, j] if t else 0.0,
                                                   self.dvol[t - 1, j] if t else np.nan, self.sim.extra_slippage_bps)
            exec_cost = (ref - fill) * qty
        value = qty * fill
        ch = self.cost.charges("SELL", value, date)
        self.cash += value - ch.total
        basis = pos.cost_basis_total * qty / pos.orig_qty
        pos.realized_net += value - ch.total - basis
        pos.realized_gross += (ref - pos.entry_ref_price) * qty
        pos.exit_charges += ch.total
        pos.exit_exec_cost += exec_cost
        pos.exit_value += value
        pos.qty -= qty
        self._record_fill(t, o, pos.symbol, "SELL", qty, ref, fill, ch, exec_cost, reason)
        if pos.qty <= 0:
            net = pos.realized_net + pos.dividends
            trade = {
                "trade_id": uuid.uuid4().hex[:12], "symbol": pos.symbol, "strategy": pos.strategy,
                "sector": pos.sector, "signal_date": pos.signal_date, "entry_date": pos.entry_date,
                "exit_date": self._date(t), "qty": pos.orig_qty, "entry_price": round(pos.entry_fill_price, 4),
                "exit_price": round(pos.exit_value / pos.orig_qty, 4),
                "gross_pnl": round(pos.realized_gross + pos.dividends, 2), "net_pnl": round(net, 2),
                "charges": round(pos.entry_charges + pos.exit_charges, 2),
                "exec_cost": round(pos.entry_exec_cost + pos.exit_exec_cost, 2),
                "dividends": round(pos.dividends, 2),
                "return_pct": round(net / pos.cost_basis_total, 6), "holding_days": pos.bars_held,
                "entry_reason": pos.entry_reason, "exit_reason": reason,
                "entry_info": pos.entry_info, "test_mode": self.sim.test_mode,
            }
            self.trades.append(trade)
            for ev in self.risk.record_trade_result(date, pos.strategy, net):
                self.alerts.append(asdict(ev))
            del self.positions[pos.symbol]

    # =================================================================== phases
    def execute(self, t: int) -> None:
        due = [o for o in self.pending if self._exec_index(o) <= t]
        if not due:
            return
        keep = [o for o in self.pending if self._exec_index(o) > t]
        p = self.p
        date = p.dates[t]
        for o in sorted(due, key=lambda x: 0 if x.side == "SELL" else 1):
            j = self.col[o.symbol]
            o.attempts += 1
            status, detail = "FILLED", ""
            up, down = self._locked(t, j)
            if not p.available[t, j]:
                status, detail = "NO_BAR", "no valid bar (halt/suspension/missing data)"
            elif o.side == "BUY" and up:
                status, detail = "CIRCUIT", "upper circuit locked - no sellers"
            elif o.side == "SELL" and down:
                status, detail = "CIRCUIT", "lower circuit locked - no buyers"
            if status != "FILLED":
                if o.side == "SELL" and o.symbol in self.positions:
                    keep.append(o)       # sells are retried next session
                    detail += " (sell retried next session)"
                self.orders_log.append({**self._order_row(o), "exec_date": str(date.date()), "status": status,
                                        "detail": detail})
                continue
            ref = p.open[t, j] if self.sim.fill_price == "open" else p.close[t, j]
            vol = p.volume[t, j] if np.isfinite(p.volume[t, j]) else 0.0
            max_qty = int(math.floor(self.sim.participation * vol))
            if o.side == "SELL":
                pos = self.positions.get(o.symbol)
                if pos is None:
                    self.orders_log.append({**self._order_row(o), "exec_date": str(date.date()),
                                            "status": "CANCELLED", "detail": "no position"})
                    continue
                q = min(pos.qty, max_qty)
                if q <= 0:
                    keep.append(o)
                    self.orders_log.append({**self._order_row(o), "exec_date": str(date.date()),
                                            "status": "NO_VOLUME", "detail": "volume cap 0; retried"})
                    continue
                self._sell(t, pos, q, ref, o.reason, o)
                st = "FILLED" if o.symbol not in self.positions else "PARTIAL"
                if st == "PARTIAL":
                    o.qty = self.positions[o.symbol].qty
                    keep.append(o)
                self.orders_log.append({**self._order_row(o), "exec_date": str(date.date()), "status": st,
                                        "detail": f"filled {q}"})
            else:
                if o.symbol in self.positions:
                    self.orders_log.append({**self._order_row(o), "exec_date": str(date.date()),
                                            "status": "CANCELLED", "detail": "already held (no pyramiding)"})
                    continue
                q = min(o.qty, max_qty)
                fill, frac = self.cost.execution_price("BUY", ref, max(q, 1), vol, self.adv[t - 1, j],
                                                       self.dvol[t - 1, j], self.sim.extra_slippage_bps)
                # cash check (never borrow)
                while q > 0:
                    ch = self.cost.charges("BUY", q * fill, date)
                    if q * fill + ch.total <= self.cash:
                        break
                    q = int(q * 0.95) if q > 20 else q - 1
                if q <= 0:
                    self.orders_log.append({**self._order_row(o), "exec_date": str(date.date()),
                                            "status": "REJECTED", "detail": "insufficient cash or volume"})
                    continue
                ch = self.cost.charges("BUY", q * fill, date)
                exec_cost = (fill - ref) * q
                self.cash -= q * fill + ch.total
                stop = fill - o.stop_distance if np.isfinite(o.stop_distance) else 0.0
                self.positions[o.symbol] = Position(
                    symbol=o.symbol, strategy=o.strategy, qty=q, orig_qty=q, entry_date=str(date.date()),
                    entry_ref_price=ref, entry_fill_price=fill, stop=stop, cost_basis_total=q * fill + ch.total,
                    entry_charges=ch.total, entry_exec_cost=exec_cost, signal_date=o.signal_date,
                    entry_reason=o.reason, entry_info=o.info, sector=str(p.sectors[j]), last_price=ref,
                )
                self._record_fill(t, o, o.symbol, "BUY", q, ref, fill, ch, exec_cost, o.reason)
                self.orders_log.append({**self._order_row(o), "exec_date": str(date.date()),
                                        "status": "FILLED" if q == o.qty else "PARTIAL",
                                        "detail": f"filled {q} of {o.qty}"})
        self.pending = keep

    def _order_row(self, o: Order) -> dict:
        return {"order_id": o.order_id, "signal_date": o.signal_date, "symbol": o.symbol, "strategy": o.strategy,
                "side": o.side, "qty": o.qty, "reason": o.reason, "attempt": o.attempts, "test_mode": o.test_mode}

    def stops(self, t: int) -> None:
        p = self.p
        for sym in list(self.positions):
            pos = self.positions[sym]
            j = self.col[sym]
            if pos.stop <= 0 or not p.available[t, j]:
                continue
            if p.low[t, j] <= pos.stop:
                _, down = self._locked(t, j)
                if down:
                    self.alerts.append({"date": self._date(t), "severity": "WARNING", "code": "STOP_BLOCKED",
                                        "detail": f"{sym} stop hit but lower circuit locked", "strategy": pos.strategy})
                    continue
                ref = min(p.open[t, j], pos.stop)
                reason = "STOP_LOSS (gap through stop, filled at open)" if p.open[t, j] < pos.stop else "STOP_LOSS"
                self._sell(t, pos, pos.qty, ref, reason)
        self.pending = [o for o in self.pending if not (o.side == "SELL" and o.symbol not in self.positions)]

    def corporate_actions(self, t: int) -> None:
        date = self._date(t)
        for sym, pos in self.positions.items():
            j = self.col[sym]
            d = self.p.dividend[t, j]
            if d > 0 and pos.entry_date < date:
                amt = d * pos.qty
                self.cash += amt
                pos.dividends += amt
                self.cum_dividends += amt

    def mark(self, t: int) -> None:
        p = self.p
        for sym in list(self.positions):
            pos = self.positions[sym]
            j = self.col[sym]
            if p.available[t, j]:
                prev = pos.last_price
                pos.last_price = float(p.close[t, j])
                pos.stale_days = 0
                if pos.entry_date < self._date(t) and prev > 0:
                    gap = p.open[t, j] / prev - 1.0
                    if gap <= -self.sim.gap_alert:
                        self.alerts.append({"date": self._date(t), "severity": "WARNING", "code": "GAP_RISK",
                                            "detail": f"{sym} opened {gap:.1%} vs previous close", "strategy": pos.strategy})
            else:
                pos.stale_days += 1
                if pos.stale_days > self.sim.stale_days:
                    self._sell(t, pos, pos.qty, pos.last_price,
                               f"WRITE_OFF: no price for {pos.stale_days} sessions (suspension/delisting)",
                               haircut=self.sim.delisting_haircut)
                    continue
            pos.bars_held += 1

    def risk_update(self, t: int) -> None:
        eq = self._equity()
        for ev in self.risk.update_after_close(self.p.dates[t], eq, self.regimes.iloc[t]):
            self.alerts.append(asdict(ev))
        invested = self._invested()
        self.equity_rows.append({
            "date": self.p.dates[t], "equity": eq, "cash": self.cash, "invested": invested,
            "exposure": invested / eq if eq > 0 else 0.0, "n_positions": len(self.positions),
            "cum_charges": self.cum_charges, "cum_exec_cost": self.cum_exec_cost,
            "cum_dividends": self.cum_dividends, "gross_equity": eq + self.cum_charges + self.cum_exec_cost,
            "regime": self.regimes.iloc[t], "kill_switch": self.risk.state.kill_switch,
        })

    def _realised_vol(self) -> float:
        n = self.risk.L.portfolio.volatility_lookback_days
        if len(self.equity_rows) < 20:
            return float("nan")
        eq = np.array([r["equity"] for r in self.equity_rows[-(n + 1):]])
        r = eq[1:] / eq[:-1] - 1.0
        return float(np.std(r, ddof=1) * np.sqrt(252))

    def decide(self, t: int) -> None:
        p = self.p
        date = p.dates[t]
        ds = self._date(t)
        regime = self.regimes.iloc[t]
        pending_syms = {o.symbol for o in self.pending}
        # ---------------- exits
        for sym, pos in list(self.positions.items()):
            if sym in pending_syms:
                continue
            j = self.col[sym]
            sig = self.signals.get(pos.strategy)
            reason = None
            if sig is not None and sig.exit[t, j]:
                reason = f"SIGNAL_EXIT: {sig.reasons.get('exit', 'exit condition')}"
            elif sig is not None and sig.max_hold and pos.bars_held >= sig.max_hold:
                reason = f"TIME_STOP: held {pos.bars_held} >= strategy max_hold {sig.max_hold}"
            elif pos.bars_held >= self.max_holding_days:
                reason = f"RISK_TIME_STOP: held {pos.bars_held} >= {self.max_holding_days}"
            if reason:
                o = Order(uuid.uuid4().hex[:12], ds, sym, pos.strategy, "SELL", pos.qty, reason,
                          test_mode=self.sim.test_mode)
                self.pending.append(o)
                pending_syms.add(sym)
                self.orders_log.append({**self._order_row(o), "exec_date": None, "status": "CREATED", "detail": ""})
        # ---------------- entries
        equity = self._equity()
        invested = self._invested()
        reserved_cash = 0.0
        reserved_value = 0.0
        sector_val: dict[str, float] = {}
        for pos in self.positions.values():
            sector_val[pos.sector] = sector_val.get(pos.sector, 0.0) + pos.qty * pos.last_price
        n_open = len(self.positions) + sum(1 for o in self.pending if o.side == "BUY")
        cap = self.risk.exposure_cap(regime, self._realised_vol())
        lookback = self.risk.L.portfolio.correlation_lookback_days
        for name, sig in self.signals.items():
            cand = np.flatnonzero(sig.entry[t] & self.universe[t])
            if cand.size == 0:
                continue
            allowed, why = self.risk.entries_allowed(date, name, regime, self.gating.get(name))
            if allowed and self.strategy_allowed and not self.strategy_allowed.get(name, False):
                allowed, why = False, "strategy research status does not permit paper entries"
            scores = sig.score[t, cand]
            order = cand[np.argsort(-np.nan_to_num(scores, nan=-np.inf), kind="stable")]
            for j in order:
                sym = p.symbols[j]
                info = {k: (round(float(v[t, j]), 4) if np.isfinite(v[t, j]) else None) for k, v in sig.info.items()}
                row = {"date": ds, "strategy": name, "symbol": sym, "side": "BUY",
                       "reason": sig.reasons.get("entry", "entry"), "score": float(np.nan_to_num(sig.score[t, j])),
                       "close": round(float(p.close[t, j]), 4), "indicators": info, "regime": regime,
                       "test_mode": self.sim.test_mode}
                if sym in self.positions or sym in pending_syms:
                    continue
                if not allowed:
                    if self.sim.record_signals:
                        self.signal_log.append({**row, "decision": "REJECTED", "detail": why})
                    continue
                stop_d = float(sig.stop_distance[t, j])
                # correlation limit
                if self.positions:
                    lo = max(0, t - lookback + 1)
                    held = [self.col[s] for s in self.positions]
                    ok, avgc = self.risk.correlation_ok(self.rets[lo:t + 1, j], self.rets[lo:t + 1][:, held])
                    if not ok:
                        if self.sim.record_signals:
                            self.signal_log.append({**row, "decision": "REJECTED",
                                                    "detail": f"avg correlation {avgc:.2f} above limit"})
                        continue
                price = float(p.close[t, j])
                dec = self.risk.size_position(
                    equity=equity, cash_available=self.cash - reserved_cash, invested_value=invested + reserved_value,
                    sector_value=sector_val.get(str(p.sectors[j]), 0.0), n_open=n_open, price=price,
                    stop_distance=stop_d, adv_value=float(np.nan_to_num(self.adv[t, j])), exposure_cap=cap)
                if not dec.approved:
                    if self.sim.record_signals:
                        self.signal_log.append({**row, "decision": "REJECTED", "detail": dec.reason,
                                                "sizing": dec.detail})
                    if dec.binding == "max_open_positions":
                        break
                    continue
                o = Order(uuid.uuid4().hex[:12], ds, sym, name, "BUY", dec.qty,
                          f"SIGNAL_ENTRY: {sig.reasons.get('entry', 'entry')}", stop_d,
                          float(np.nan_to_num(sig.score[t, j])), info, test_mode=self.sim.test_mode)
                self.pending.append(o)
                pending_syms.add(sym)
                n_open += 1
                val = dec.qty * price
                reserved_cash += val * 1.003
                reserved_value += val
                sector_val[str(p.sectors[j])] = sector_val.get(str(p.sectors[j]), 0.0) + val
                self.orders_log.append({**self._order_row(o), "exec_date": None, "status": "CREATED",
                                        "detail": f"binding={dec.binding}"})
                if self.sim.record_signals:
                    self.signal_log.append({**row, "decision": "ORDER", "detail": f"qty={dec.qty} binding={dec.binding}",
                                            "sizing": dec.detail, "order_id": o.order_id})

    def step(self, t: int, decide: bool = True) -> None:
        self.execute(t)
        self.stops(t)
        self.corporate_actions(t)
        self.mark(t)
        self.risk_update(t)
        if decide:
            self.decide(t)

    # =================================================================== persistence
    def state_dict(self) -> dict:
        return {
            "cash": self.cash,
            "positions": {k: asdict(v) for k, v in self.positions.items()},
            "pending": [asdict(o) for o in self.pending],
            "cum_charges": self.cum_charges, "cum_exec_cost": self.cum_exec_cost,
            "cum_dividends": self.cum_dividends, "turnover": self.turnover,
            "risk": self.risk.state.to_dict(),
            "equity_tail": [{**r, "date": str(pd.Timestamp(r["date"]).date())} for r in self.equity_rows[-80:]],
        }

    def load_state(self, d: dict) -> None:
        self.cash = d["cash"]
        self.positions = {k: Position(**v) for k, v in d["positions"].items()}
        self.pending = [Order(**o) for o in d["pending"]]
        self.cum_charges = d["cum_charges"]
        self.cum_exec_cost = d["cum_exec_cost"]
        self.cum_dividends = d.get("cum_dividends", 0.0)
        self.turnover = d.get("turnover", 0.0)
        self.equity_rows = [{**r, "date": pd.Timestamp(r["date"])} for r in d.get("equity_tail", [])]
