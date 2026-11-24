"""SYNTHETIC market data generator - FOR SOFTWARE TESTING ONLY.

The generated data contain NO deliberately embedded alpha: prices follow a regime-switching
factor model (market + sector + idiosyncratic noise). Any strategy that appears profitable on
this data is profiting from chance, which makes the dataset a useful check that the validation
framework rejects noise. Results produced from this data must NEVER be presented as evidence
of historical profitability. Every file name contains 'synthetic', provenance records
`is_synthetic = TRUE` and all reports carry a SYNTHETIC watermark.

Features exercised: splits, bonus issues, a rights issue, dividends, a symbol change,
delistings (including a collapse), point-in-time index membership, price bands with locked
circuit days, a block of missing bars, and holidays.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

SECTORS = ["FINANCIALS", "IT", "ENERGY", "FMCG", "AUTO", "PHARMA", "METALS", "INDUSTRIALS"]
BENCH = "SYNTH_INDEX"


def _regimes(n: int, rng: np.random.Generator) -> np.ndarray:
    # states: 0 bull, 1 sideways, 2 bear, 3 crisis
    P = np.array([
        [0.990, 0.007, 0.002, 0.001],
        [0.008, 0.985, 0.006, 0.001],
        [0.004, 0.010, 0.982, 0.004],
        [0.010, 0.010, 0.020, 0.960],
    ])
    s = np.zeros(n, dtype=int)
    for t in range(1, n):
        s[t] = rng.choice(4, p=P[s[t - 1]])
    return s


def generate(out_dir: Path | str, n_symbols: int = 60, start: str = "2010-01-04", end: str = "2026-11-20",
             seed: int = 7) -> dict[str, Path]:
    rng = np.random.default_rng(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    bdays = pd.bdate_range(start, end)
    hol_idx = []
    for _yr, g in pd.Series(bdays).groupby(bdays.year):
        hol_idx += list(rng.choice(g.index.to_numpy(), size=min(12, len(g)), replace=False))
    holidays = bdays[np.sort(hol_idx)]
    dates = bdays.difference(holidays)
    T = len(dates)

    reg = _regimes(T, rng)
    mu = np.array([0.0007, 0.0001, -0.0006, -0.0015])[reg]
    sig = np.array([0.008, 0.009, 0.014, 0.028])[reg]
    mkt = mu + sig * rng.standard_t(5, T) / np.sqrt(5 / 3)

    sec_of = np.array([SECTORS[i % len(SECTORS)] for i in range(n_symbols)])
    sec_ret = {s: 0.006 * rng.standard_normal(T) for s in SECTORS}
    beta = rng.uniform(0.6, 1.4, n_symbols)
    idio_vol = rng.uniform(0.010, 0.025, n_symbols)
    size = np.exp(rng.normal(0, 1.0, n_symbols))  # relative size -> liquidity

    rets = np.zeros((T, n_symbols))
    for i in range(n_symbols):
        rets[:, i] = beta[i] * mkt + sec_ret[sec_of[i]] + idio_vol[i] * rng.standard_t(4, T) / np.sqrt(2)

    syms = [f"SYN{i:03d}" for i in range(n_symbols)]
    # listing / delisting: some late listings, two delistings
    first = np.zeros(n_symbols, dtype=int)
    late = rng.choice(n_symbols, size=8, replace=False)
    first[late] = rng.integers(250, T // 2, size=8)
    last = np.full(n_symbols, T - 1)
    delisted = [i for i in range(n_symbols) if i not in late][:2]
    last[delisted[0]] = int(T * 0.55)
    last[delisted[1]] = int(T * 0.70)
    # collapse before delisting
    rets[last[delisted[1]] - 60 : last[delisted[1]] + 1, delisted[1]] -= 0.015

    # price bands: large caps (top 25 by size) have none (derivative stocks); others 20/10/5%
    order = np.argsort(-size)
    band = np.full(n_symbols, np.nan)
    n_large = min(25, n_symbols // 2)
    for rank, i in enumerate(order):
        if rank >= n_large:
            band[i] = [0.20, 0.10, 0.05][rank % 3]

    rows, bands_rows = [], []
    adj_close_all = np.full((T, n_symbols), np.nan)
    for i, s in enumerate(syms):
        r = rets[:, i].copy()
        locked = np.zeros(T, dtype=bool)
        if np.isfinite(band[i]):
            hit = np.abs(r) >= band[i]
            r[hit] = np.sign(r[hit]) * band[i]
            locked = hit
        px = 100 * rng.uniform(0.5, 20) * np.exp(np.cumsum(r))
        prev = np.r_[px[0], px[:-1]]
        gap = np.clip(0.4 * r + 0.003 * rng.standard_normal(T), -0.2, 0.2)
        o = prev * np.exp(gap)
        if np.isfinite(band[i]):
            o = np.clip(o, prev * (1 - band[i]), prev * (1 + band[i]))
        hi = np.maximum(o, px) * (1 + np.abs(0.006 * rng.standard_normal(T)))
        lo = np.minimum(o, px) * (1 - np.abs(0.006 * rng.standard_normal(T)))
        o[locked], hi[locked], lo[locked] = px[locked], px[locked], px[locked]
        base_vol = 2e5 * size[i] * rng.uniform(0.5, 2.0)
        vol = np.maximum(1.0, base_vol * np.exp(0.4 * rng.standard_normal(T)) * (1 + 20 * np.abs(r)))
        vol[locked] *= 0.1
        span = slice(first[i], last[i] + 1)
        adj_close_all[span, i] = px[span]
        for t in range(first[i], last[i] + 1):
            rows.append((dates[t], s, o[t], hi[t], lo[t], px[t], vol[t]))
        if np.isfinite(band[i]):
            bands_rows.append({"symbol": s, "start_date": dates[0].date(), "band_pct": band[i],
                               "end_date": None, "exchange": "NSE"})
    adj = pd.DataFrame(rows, columns=["date", "symbol", "open", "high", "low", "close", "volume"])

    # ---- corporate actions on the adjusted series -> derive raw (unadjusted) prices
    acts = []
    def pick_date(i, lo_frac, hi_frac):
        a, b = max(first[i] + 300, int(T * lo_frac)), min(last[i] - 50, int(T * hi_frac))
        return dates[int(rng.integers(a, max(a + 1, b)))]
    candidates = [i for i in range(n_symbols) if i not in delisted and i not in late]
    for i in candidates[2:5]:
        acts.append({"symbol": syms[i], "ex_date": pick_date(i, 0.2, 0.9), "action_type": "split",
                     "ratio_from": 1, "ratio_to": 5})
    for i in candidates[5:7]:
        acts.append({"symbol": syms[i], "ex_date": pick_date(i, 0.2, 0.9), "action_type": "bonus",
                     "ratio_from": 1, "ratio_to": 1})
    for i in candidates[:20]:
        for _ in range(int(rng.integers(3, 8))):
            acts.append({"symbol": syms[i], "ex_date": pick_date(i, 0.05, 0.98), "action_type": "dividend",
                         "cash_amount": None})
    ri = candidates[7]
    acts.append({"symbol": syms[ri], "ex_date": pick_date(ri, 0.3, 0.8), "action_type": "rights",
                 "ratio_from": 5, "ratio_to": 1, "rights_price": None})
    acts = pd.DataFrame(acts).drop_duplicates(["symbol", "ex_date", "action_type"])

    raw = adj.copy()
    for a in acts.sort_values("ex_date").itertuples(index=False):
        m = (raw["symbol"] == a.symbol)
        before = m & (raw["date"] < a.ex_date)
        if a.action_type == "split":
            f = a.ratio_to / a.ratio_from
        elif a.action_type == "bonus":
            f = (a.ratio_from + a.ratio_to) / a.ratio_from
        elif a.action_type == "rights":
            p_cum = float(raw.loc[before, "close"].iloc[-1])
            rp = round(0.7 * p_cum, 2)
            acts.loc[(acts["symbol"] == a.symbol) & (acts["action_type"] == "rights"), "rights_price"] = rp
            terp = (a.ratio_from * p_cum + a.ratio_to * rp) / (a.ratio_from + a.ratio_to)
            f = p_cum / terp
        else:
            continue
        for c in ("open", "high", "low", "close"):
            raw.loc[before, c] *= f
        raw.loc[before, "volume"] /= f
    # dividends ~1% of raw price on ex-date
    for idx in acts.index[acts["action_type"] == "dividend"]:
        a = acts.loc[idx]
        px = raw.loc[(raw["symbol"] == a.symbol) & (raw["date"] == a.ex_date), "close"]
        acts.loc[idx, "cash_amount"] = round(float(px.iloc[0]) * 0.01, 2) if len(px) else 1.0

    # symbol change: SYN010 renamed to SYN010N half-way
    sc_sym = syms[candidates[10]]
    sc_date = dates[T // 2]
    raw.loc[(raw["symbol"] == sc_sym) & (raw["date"] >= sc_date), "symbol"] = sc_sym + "N"
    acts = pd.concat([acts, pd.DataFrame([{"symbol": sc_sym, "ex_date": sc_date, "action_type": "symbol_change",
                                           "new_symbol": sc_sym + "N"}])], ignore_index=True)

    # data defect for DQ testing: remove a block of 120 bars for one symbol
    gap_sym = syms[candidates[12]]
    gd = raw[raw["symbol"] == gap_sym]["date"]
    gap_dates = gd.iloc[len(gd) // 3 : len(gd) // 3 + 120]
    raw = raw[~((raw["symbol"] == gap_sym) & raw["date"].isin(gap_dates))]

    raw["exchange"] = "NSE"
    for c in ("open", "high", "low", "close"):
        raw[c] = raw[c].round(2)
    raw["volume"] = raw["volume"].round(0)
    raw["value"] = (raw["close"] * raw["volume"]).round(0)

    # benchmark: cap-weighted-ish index of adjusted prices (synthetic)
    w = size / size.sum()
    ret_mat = np.nan_to_num(pd.DataFrame(adj_close_all).pct_change().to_numpy(), nan=0.0)
    idx_level = 10000 * np.cumprod(1 + ret_mat @ w)
    bench = pd.DataFrame({"date": dates, "index_name": BENCH, "close": idx_level.round(2)})
    bench["open"] = bench["close"].shift(1).fillna(bench["close"])
    bench["high"] = bench[["open", "close"]].max(axis=1)
    bench["low"] = bench[["open", "close"]].min(axis=1)

    secs = pd.DataFrame({
        "symbol": [s + ("N" if s == sc_sym else "") for s in syms], "exchange": "NSE",
        "isin": [f"INSYN{i:07d}" for i in range(n_symbols)], "name": [f"Synthetic Co {i}" for i in range(n_symbols)],
        "sector": sec_of, "industry": None, "listing_date": [dates[f].date() for f in first],
        "delisting_date": [dates[last[i]].date() if i in delisted else None for i in range(n_symbols)],
        "delisting_reason": ["synthetic delisting" if i in delisted else None for i in range(n_symbols)],
    })

    # point-in-time membership: top 30 by trailing 1y traded value at each year start
    raw_val = raw.pivot_table(index="date", columns="symbol", values="value")
    mem_rows = []
    years = sorted(set(dates.year))
    prev_members: set[str] = set()
    since: dict[str, pd.Timestamp] = {}
    for y in years[1:]:
        ystart = dates[dates.year == y][0]
        window = raw_val[(raw_val.index < ystart) & (raw_val.index >= ystart - pd.Timedelta(days=365))]
        alive = [s for s in window.columns if s in raw[raw["date"] >= ystart]["symbol"].unique()]
        top = set(window[alive].median().sort_values(ascending=False).head(30).index)
        for s in prev_members - top:
            mem_rows.append({"index_name": "SYNTH30", "symbol": s, "start_date": since[s].date(),
                             "end_date": (ystart - pd.Timedelta(days=1)).date()})
        for s in top - prev_members:
            since[s] = ystart
        prev_members = top
    for s in prev_members:
        mem_rows.append({"index_name": "SYNTH30", "symbol": s, "start_date": since[s].date(), "end_date": None})
    membership = pd.DataFrame(mem_rows)

    hol = pd.DataFrame({"date": holidays.date, "exchange": "NSE", "description": "synthetic holiday",
                        "source": "synthetic generator", "verified": True})

    paths = {
        "prices": out / "synthetic_prices.csv",
        "securities": out / "synthetic_securities.csv",
        "corporate_actions": out / "synthetic_corporate_actions.csv",
        "index_membership": out / "synthetic_index_membership.csv",
        "benchmarks": out / "synthetic_benchmark.csv",
        "holidays": out / "synthetic_holidays.csv",
        "price_bands": out / "synthetic_price_bands.csv",
    }
    raw.sort_values(["symbol", "date"]).to_csv(paths["prices"], index=False, date_format="%Y-%m-%d")
    secs.to_csv(paths["securities"], index=False)
    acts.to_csv(paths["corporate_actions"], index=False, date_format="%Y-%m-%d")
    membership.to_csv(paths["index_membership"], index=False)
    bench.to_csv(paths["benchmarks"], index=False, date_format="%Y-%m-%d")
    hol.to_csv(paths["holidays"], index=False)
    pd.DataFrame(bands_rows, columns=["symbol", "start_date", "band_pct", "end_date", "exchange"]).to_csv(
        paths["price_bands"], index=False)
    (out / "README_SYNTHETIC.txt").write_text(
        "SYNTHETIC DATA FOR SOFTWARE TESTING ONLY. Not market data. No alpha embedded.\n"
        "Never use results on this data as evidence of profitability.\n"
    )
    return paths
