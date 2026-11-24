"""Indian cash-equity (delivery) transaction-cost and execution-price model.

Statutory/exchange charges are looked up by trade date from config/transaction_costs.yaml.
Execution costs (spread, slippage, market impact) are modelled separately and can be stressed.

cost components per fill (INR):
  brokerage   = clip(pct * value, min, max)
  stt         = rate_side * value                         (buy and sell for delivery)
  exch_txn    = rate(exchange, date) * value
  ipft        = rate(exchange, date) * value
  sebi_fee    = rate(date) * value
  stamp_duty  = rate(date) * value                         (buy side only)
  gst         = gst_rate * (brokerage + exch_txn + sebi_fee + ipft)
  dp_charge   = flat INR per scrip per sell day             (sell side only)
execution price:
  buy  fill = ref_price * (1 + half_spread + slippage + impact)
  sell fill = ref_price * (1 - half_spread - slippage - impact)
  impact    = coef * sigma_daily * sqrt(order_qty / bar_volume)
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class CostBreakdown:
    brokerage: float = 0.0
    stt: float = 0.0
    exchange_txn: float = 0.0
    ipft: float = 0.0
    sebi_fee: float = 0.0
    stamp_duty: float = 0.0
    gst: float = 0.0
    dp_charge: float = 0.0

    @property
    def total(self) -> float:
        return (self.brokerage + self.stt + self.exchange_txn + self.ipft + self.sebi_fee + self.stamp_duty
                + self.gst + self.dp_charge)

    def as_dict(self) -> dict:
        d = {k: round(v, 4) for k, v in self.__dict__.items()}
        d["total"] = round(self.total, 4)
        return d


class _Schedule:
    def __init__(self, entries: list[dict]):
        rows = sorted(entries, key=lambda e: str(e["effective_from"]))
        self.dates = [pd.Timestamp(str(e["effective_from"])) for e in rows]
        self.rows = rows

    def at(self, d: pd.Timestamp) -> dict:
        i = bisect.bisect_right(self.dates, pd.Timestamp(d)) - 1
        if i < 0:
            i = 0  # before first entry: use earliest available (flagged as approximation in docs)
        return self.rows[i]


@dataclass
class CostModel:
    cfg: dict
    cost_multiplier: float = 1.0          # stress statutory+brokerage costs
    slippage_multiplier: float = 1.0      # stress spread+slippage+impact
    _s: dict = field(default_factory=dict, init=False)

    def __post_init__(self):
        c = self.cfg
        self._s = {
            "brokerage": _Schedule(c["brokerage"]),
            "stt": _Schedule(c["stt"]),
            "sebi": _Schedule(c["sebi_turnover_fee"]),
            "stamp": _Schedule(c["stamp_duty"]),
            "gst": _Schedule(c["gst"]),
            "dp": _Schedule(c.get("dp_charges", [{"effective_from": "2000-01-01", "inr_per_sell": 0.0}])),
            "exch": {ex: _Schedule(v) for ex, v in c["exchange_txn"].items()},
            "ipft": {ex: _Schedule(v) for ex, v in c.get("ipft", {}).items()},
        }
        ex = c["execution"]
        self._spread = sorted(ex["half_spread_bps_by_adv_crore"], key=lambda r: -r["min_adv_crore"])
        self._slip_bps = float(ex["base_slippage_bps"])
        self._impact = float(ex["impact_coefficient"])

    def with_stress(self, cost_multiplier: float = 1.0, slippage_multiplier: float = 1.0) -> CostModel:
        return CostModel(self.cfg, cost_multiplier, slippage_multiplier)

    # ------------------------------------------------------------- statutory/explicit
    def charges(self, side: str, value: float, date, exchange: str = "NSE") -> CostBreakdown:
        if value <= 0:
            return CostBreakdown()
        d = pd.Timestamp(date)
        b = self._s["brokerage"].at(d)
        brokerage = value * float(b["pct"])
        brokerage = max(brokerage, float(b.get("min_per_order") or 0.0))
        if b.get("max_per_order") is not None:
            brokerage = min(brokerage, float(b["max_per_order"]))
        stt_row = self._s["stt"].at(d)
        stt = value * float(stt_row["buy_pct"] if side == "BUY" else stt_row["sell_pct"])
        exch = value * float(self._s["exch"].get(exchange, self._s["exch"]["NSE"]).at(d)["pct"])
        ipft_s = self._s["ipft"].get(exchange)
        ipft = value * float(ipft_s.at(d)["pct"]) if ipft_s else 0.0
        sebi = value * float(self._s["sebi"].at(d)["pct"])
        stamp = value * float(self._s["stamp"].at(d)["buy_pct"]) if side == "BUY" else 0.0
        gst = float(self._s["gst"].at(d)["rate"]) * (brokerage + exch + sebi + ipft)
        dp = float(self._s["dp"].at(d)["inr_per_sell"]) if side == "SELL" else 0.0
        m = self.cost_multiplier
        return CostBreakdown(brokerage * m, stt * m, exch * m, ipft * m, sebi * m, stamp * m, gst * m, dp * m)

    def round_trip_pct(self, value: float, date, exchange: str = "NSE") -> float:
        return (self.charges("BUY", value, date, exchange).total + self.charges("SELL", value, date, exchange).total) / value

    # ------------------------------------------------------------- execution price
    def half_spread(self, adv_inr: float) -> float:
        adv_cr = (adv_inr or 0.0) / 1e7
        for r in self._spread:
            if adv_cr >= r["min_adv_crore"]:
                return r["bps"] / 1e4
        return self._spread[-1]["bps"] / 1e4

    def execution_price(self, side: str, ref_price: float, qty: float, bar_volume: float, adv_inr: float,
                        daily_vol: float, extra_bps: float = 0.0) -> tuple[float, float]:
        """Return (fill_price, execution_cost_fraction)."""
        part = qty / bar_volume if bar_volume and bar_volume > 0 else 1.0
        sigma = daily_vol if np.isfinite(daily_vol) and daily_vol > 0 else 0.02
        frac = (self.half_spread(adv_inr) + self._slip_bps / 1e4 + self._impact * sigma * np.sqrt(part)
                + extra_bps / 1e4) * self.slippage_multiplier
        px = ref_price * (1 + frac) if side == "BUY" else ref_price * (1 - frac)
        return px, frac
