"""Aligned date x symbol market panel used by strategies, the backtester and paper trading.

All arrays are shaped (T, N). Missing bars remain NaN (never filled). `available[t, i]`
is True only for a valid bar.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .corporate_actions import adjust_prices, resolve_symbol_changes


@dataclass
class Panel:
    dates: pd.DatetimeIndex
    symbols: list[str]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    raw_close: np.ndarray
    volume: np.ndarray
    value: np.ndarray
    dividend: np.ndarray
    available: np.ndarray
    sectors: np.ndarray
    benchmark: pd.Series
    members: np.ndarray | None = None          # point-in-time index membership mask
    delist_dates: dict = field(default_factory=dict)
    band_pct: np.ndarray | None = None         # known price band per bar (fraction), NaN if unknown
    survivorship_safe: bool = False
    synthetic: bool = False
    meta: dict = field(default_factory=dict)

    @property
    def T(self) -> int:
        return len(self.dates)

    @property
    def N(self) -> int:
        return len(self.symbols)

    def frame(self, name: str) -> pd.DataFrame:
        return pd.DataFrame(getattr(self, name), index=self.dates, columns=self.symbols)

    def returns(self) -> np.ndarray:
        with np.errstate(divide="ignore", invalid="ignore"):
            r = np.full_like(self.close, np.nan)
            r[1:] = self.close[1:] / self.close[:-1] - 1.0
        return r

    def index_of(self, date) -> int:
        return int(self.dates.get_indexer([pd.Timestamp(date)])[0])

    def truncate(self, end) -> Panel:
        """Panel containing only data up to and including `end` (used by leakage tests)."""
        k = int(np.searchsorted(self.dates.values, np.datetime64(pd.Timestamp(end)), side="right"))
        def cut(a):
            return None if a is None else a[:k].copy()
        return Panel(
            dates=self.dates[:k], symbols=list(self.symbols), open=cut(self.open), high=cut(self.high),
            low=cut(self.low), close=cut(self.close), raw_close=cut(self.raw_close), volume=cut(self.volume),
            value=cut(self.value), dividend=cut(self.dividend), available=cut(self.available),
            sectors=self.sectors.copy(), benchmark=self.benchmark.iloc[:k].copy(), members=cut(self.members),
            delist_dates=dict(self.delist_dates), band_pct=cut(self.band_pct),
            survivorship_safe=self.survivorship_safe, synthetic=self.synthetic, meta=dict(self.meta),
        )

    def universe_mask(self, min_value_inr: float, min_price: float, min_history: int,
                      top_n: int | None = None, rank_window: int = 126) -> np.ndarray:
        """Causal eligibility mask: liquidity, raw-price floor, listing age, PIT membership and
        (optionally) the top-N stocks by trailing median traded value - a point-in-time universe
        built only from data available on each date."""
        val = pd.DataFrame(self.value, index=self.dates)
        med_val = val.rolling(20, min_periods=15).median().to_numpy()
        hist = np.cumsum(self.available, axis=0)
        with np.errstate(invalid="ignore"):
            mask = (
                self.available
                & (np.nan_to_num(med_val, nan=0.0) >= min_value_inr)
                & (np.nan_to_num(self.raw_close, nan=0.0) >= min_price)
                & (hist >= min_history)
            )
        if self.members is not None:
            mask &= self.members
        if top_n:
            liq = val.rolling(rank_window, min_periods=max(20, rank_window // 2)).median().to_numpy()
            liq = np.where(mask, liq, np.nan)
            rank = pd.DataFrame(liq).rank(axis=1, ascending=False, method="first").to_numpy()
            mask &= np.nan_to_num(rank, nan=np.inf) <= top_n
        return mask


def build_panel(
    prices: pd.DataFrame,
    benchmark: pd.DataFrame,
    securities: pd.DataFrame | None = None,
    actions: pd.DataFrame | None = None,
    membership: pd.DataFrame | None = None,
    universe_index: str | None = None,
    price_bands: pd.DataFrame | None = None,
    exclude_symbols: list[str] | None = None,
    synthetic: bool = False,
    exclude_rows: pd.DataFrame | None = None,
    complete_exchange_history: bool = False,
) -> Panel:
    """`complete_exchange_history`: prices come from the exchange's complete daily files (every traded
    security, including later-delisted ones), so a causally-ranked liquidity universe is survivorship-safe."""
    if benchmark is None or benchmark.empty:
        raise ValueError("benchmark series is required to build a panel")
    prices = prices.copy()
    if exclude_symbols:
        prices = prices[~prices["symbol"].isin(exclude_symbols)]
    if exclude_rows is not None and len(exclude_rows):
        bad = pd.MultiIndex.from_frame(exclude_rows[["date", "symbol"]].assign(date=lambda x: pd.to_datetime(x["date"])))
        prices = prices[~pd.MultiIndex.from_frame(prices[["date", "symbol"]]).isin(bad)]
    actions = actions if actions is not None else pd.DataFrame(
        columns=["symbol", "ex_date", "action_type", "ratio_from", "ratio_to", "cash_amount", "rights_price",
                 "new_symbol", "adjustment_factor"]
    )
    adj = adjust_prices(prices, actions)
    _, sym_map = resolve_symbol_changes(pd.DataFrame({"symbol": []}), actions) if len(actions) else (None, {})

    bench = benchmark.sort_values("date").drop_duplicates("date").set_index("date")["close"]
    dates = pd.DatetimeIndex(bench.index)
    dates = dates[(dates >= adj["date"].min()) & (dates <= adj["date"].max())]
    bench = bench.reindex(dates)
    symbols = sorted(adj["symbol"].unique())

    adj = adj.drop_duplicates(["date", "symbol"], keep="first")

    def pivot(col):
        return adj.pivot(index="date", columns="symbol", values=col).reindex(
            index=dates, columns=symbols
        ).to_numpy(dtype=float)

    o, h, lo, c = pivot("adj_open"), pivot("adj_high"), pivot("adj_low"), pivot("adj_close")
    raw_c, vol, val, div = pivot("close"), pivot("adj_volume"), pivot("value"), pivot("dividend_adj")
    div = np.nan_to_num(div, nan=0.0)
    available = np.isfinite(o) & np.isfinite(h) & np.isfinite(lo) & np.isfinite(c) & (c > 0)

    sectors = np.array(["UNKNOWN"] * len(symbols), dtype=object)
    delist = {}
    if securities is not None and not securities.empty:
        sec = securities.copy()
        sec["symbol"] = sec["symbol"].replace(sym_map)
        sec = sec.drop_duplicates("symbol", keep="last").set_index("symbol")
        for j, s in enumerate(symbols):
            if s in sec.index:
                sectors[j] = sec.at[s, "sector"] or "UNKNOWN"
                dl = sec.at[s, "delisting_date"]
                if pd.notna(dl):
                    delist[s] = pd.Timestamp(dl)

    members = None
    survivorship_safe = False
    if membership is not None and not membership.empty and universe_index:
        m = membership[membership["index_name"] == universe_index].copy()
        m["symbol"] = m["symbol"].replace(sym_map)
        members = np.zeros((len(dates), len(symbols)), dtype=bool)
        col = {s: j for j, s in enumerate(symbols)}
        for r in m.itertuples(index=False):
            if r.symbol not in col:
                continue
            end = pd.Timestamp(r.end_date) if pd.notna(r.end_date) else dates.max()
            members[(dates >= pd.Timestamp(r.start_date)) & (dates <= end), col[r.symbol]] = True
        # PIT membership AND delisted securities present -> treated as survivorship-safe
        survivorship_safe = bool(len(delist) > 0 or m["end_date"].notna().any())
    elif complete_exchange_history:
        survivorship_safe = True

    band = None
    if price_bands is not None and not price_bands.empty:
        band = np.full((len(dates), len(symbols)), np.nan)
        col = {s: j for j, s in enumerate(symbols)}
        for r in price_bands.sort_values("start_date").itertuples(index=False):
            if r.symbol in col:
                end = pd.Timestamp(r.end_date) if pd.notna(r.end_date) else dates.max()
                band[(dates >= pd.Timestamp(r.start_date)) & (dates <= end), col[r.symbol]] = r.band_pct

    return Panel(
        dates=dates, symbols=symbols, open=o, high=h, low=lo, close=c, raw_close=raw_c, volume=vol,
        value=val, dividend=div, available=available, sectors=sectors, benchmark=bench, members=members,
        delist_dates=delist, band_pct=band, survivorship_safe=survivorship_safe, synthetic=synthetic,
        meta={"universe_index": universe_index, "symbol_map": sym_map,
              "complete_exchange_history": complete_exchange_history},
    )


def load_panel_from_store(store, cfg, exclude_symbols: list[str] | None = None,
                          universe_index: str | None = None, exclude_rows: pd.DataFrame | None = None) -> Panel:
    d = cfg.settings.data
    bm_name = cfg.settings.backtest.benchmark
    universe_index = universe_index or (d.universe_index if d.universe_mode == "index_membership" else None)
    symbols = store.liquid_symbols(d.min_median_traded_value_inr) if d.prefilter_liquid_symbols else None
    prices = store.prices(exchange="NSE", symbols=symbols)
    bench = store.benchmark(bm_name)
    from .exchange_sources import NSE_SOURCE

    complete = any(s.startswith(NSE_SOURCE) for s in store.source_names("prices"))
    return build_panel(
        prices,
        bench,
        securities=store.table("securities"),
        actions=store.table("corporate_actions"),
        membership=store.table("index_membership"),
        universe_index=universe_index,
        price_bands=store.table("price_bands"),
        exclude_symbols=exclude_symbols,
        synthetic=store.any_synthetic(),
        exclude_rows=exclude_rows,
        complete_exchange_history=complete and d.universe_mode == "liquidity_top_n",
    )
