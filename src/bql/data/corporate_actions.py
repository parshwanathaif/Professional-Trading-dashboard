"""Corporate-action processing: price/volume adjustment factors, dividends, symbol changes.

Conventions
-----------
* A price factor f >= 1 for an event with ex-date E means: prices strictly BEFORE E are
  divided by f, volumes multiplied by f, so the adjusted series is continuous across E.
* Cumulative factor F(t) = product of f over all events with ex-date > t.
  adjusted_price(t) = raw_price(t) / F(t).
* Split  "ratio_from:ratio_to"  (1:5 means 1 old share -> 5 new):  f = to / from
* Bonus  "ratio_from:ratio_to"  (2:1 means 1 bonus for every 2 held): f = (from + to) / from
* Rights "ratio_from:ratio_to" at rights_price R, cum-rights close P (day before ex-date):
       TERP = (from*P + to*R) / (from+to);  f = P / TERP
* Demerger / other: explicit `adjustment_factor` supplied by the data provider.
* Dividends do NOT adjust prices; they are credited as cash to holders on the ex-date
  (amount per adjusted share = cash_amount / F(ex_date)).
* Symbol changes are resolved to the latest symbol so one security has one continuous series.
* Mergers: the series ends at the last traded date; open positions are closed by the
  backtester's delisting logic (documented limitation: share-swap consideration is not modelled
  unless the provider supplies it).

Back-adjustment uses later corporate-action ratios to rescale earlier prices. This does not
leak return information (returns are unchanged except across the event, where they become
correct), but any rule that uses absolute price LEVELS (e.g. a minimum-price filter) must use
raw prices. The panel builder exposes both.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def resolve_symbol_changes(prices: pd.DataFrame, actions: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Rename old symbols to their final symbol. Returns (prices, mapping old->final)."""
    sc = actions[actions["action_type"] == "symbol_change"].dropna(subset=["new_symbol"])
    mapping = dict(zip(sc["symbol"], sc["new_symbol"]))
    final = {}
    for old in mapping:
        seen, cur = {old}, mapping[old]
        while cur in mapping and cur not in seen:
            seen.add(cur)
            cur = mapping[cur]
        final[old] = cur
    if final:
        prices = prices.copy()
        prices["symbol"] = prices["symbol"].replace(final)
    return prices, final


def event_factors(prices: pd.DataFrame, actions: pd.DataFrame) -> pd.DataFrame:
    """Return DataFrame[symbol, ex_date, factor, action_type] for price-adjusting events."""
    rows = []
    px = prices.set_index(["symbol", "date"])["close"].sort_index()
    for r in actions.itertuples(index=False):
        t = r.action_type
        f = np.nan
        if t == "split" and r.ratio_from and r.ratio_to:
            f = float(r.ratio_to) / float(r.ratio_from)
        elif t == "bonus" and r.ratio_from and r.ratio_to:
            f = (float(r.ratio_from) + float(r.ratio_to)) / float(r.ratio_from)
        elif t == "rights" and r.ratio_from and r.ratio_to and r.rights_price is not None:
            try:
                s = px.loc[r.symbol]
                prior = s[s.index < r.ex_date]
                if len(prior):
                    p = float(prior.iloc[-1])
                    terp = (r.ratio_from * p + r.ratio_to * r.rights_price) / (r.ratio_from + r.ratio_to)
                    f = p / terp if terp > 0 else np.nan
            except KeyError:
                pass
        elif t in ("demerger", "merger") and r.adjustment_factor and r.adjustment_factor > 0:
            f = float(r.adjustment_factor)
        elif r.adjustment_factor and r.adjustment_factor > 0 and t not in ("dividend", "symbol_change"):
            f = float(r.adjustment_factor)
        if np.isfinite(f) and f > 0 and abs(f - 1.0) > 1e-12:
            rows.append({"symbol": r.symbol, "ex_date": r.ex_date, "factor": f, "action_type": t})
    return pd.DataFrame(rows, columns=["symbol", "ex_date", "factor", "action_type"])


def cumulative_factor(dates: pd.DatetimeIndex, events: pd.DataFrame) -> np.ndarray:
    """F(t) for one symbol: product of factors with ex_date > t."""
    F = np.ones(len(dates))
    for ex, f in zip(events["ex_date"], events["factor"]):
        F[dates < pd.Timestamp(ex)] *= f
    return F


def adjust_prices(prices: pd.DataFrame, actions: pd.DataFrame) -> pd.DataFrame:
    """Add adj_open/high/low/close, adj_volume, adj_factor and dividend_adj columns."""
    if actions is None or actions.empty:
        out = prices.copy()
        out["adj_factor"] = 1.0
        for c in ("open", "high", "low", "close"):
            out[f"adj_{c}"] = out[c]
        out["adj_volume"] = out["volume"]
        out["dividend_adj"] = 0.0
        return out
    prices, _ = resolve_symbol_changes(prices, actions)
    acts = actions.copy()
    _, sym_map = resolve_symbol_changes(pd.DataFrame({"symbol": []}), actions)
    acts["symbol"] = acts["symbol"].replace(sym_map)
    ev = event_factors(prices, acts)
    parts = []
    for sym, g in prices.groupby("symbol", sort=False):
        g = g.sort_values("date").copy()
        e = ev[ev["symbol"] == sym]
        F = cumulative_factor(pd.DatetimeIndex(g["date"]), e) if len(e) else np.ones(len(g))
        g["adj_factor"] = F
        for c in ("open", "high", "low", "close"):
            g[f"adj_{c}"] = g[c] / F
        g["adj_volume"] = g["volume"] * F
        g["dividend_adj"] = 0.0
        divs = acts[(acts["symbol"] == sym) & (acts["action_type"] == "dividend")]
        for d in divs.itertuples(index=False):
            if d.cash_amount is None or not np.isfinite(d.cash_amount):
                continue
            idx = g.index[g["date"] == d.ex_date]
            if len(idx):
                g.loc[idx, "dividend_adj"] += d.cash_amount / g.loc[idx, "adj_factor"].to_numpy()
        parts.append(g)
    return pd.concat(parts).sort_values(["symbol", "date"]).reset_index(drop=True)
