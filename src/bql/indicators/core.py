"""Causal technical indicators on (T, N) arrays.

Every function uses only data at or before row t to compute row t (rolling / expanding /
recursive filters). NaN inputs propagate; nothing is forward-filled. The leakage test in
`bql.validation.leakage` verifies causality of complete strategies end to end.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _df(a: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame(a)


def _mp(n: int) -> int:
    """Minimum observations in a window: tolerate up to 10% missing bars (never filled)."""
    return max(2, int(np.ceil(0.9 * n)))


def sma(x: np.ndarray, n: int, min_periods: int | None = None) -> np.ndarray:
    return _df(x).rolling(n, min_periods=min_periods or _mp(n)).mean().to_numpy()


def ema(x: np.ndarray, n: int) -> np.ndarray:
    return _df(x).ewm(span=n, adjust=False, min_periods=n).mean().to_numpy()


def rolling_std(x: np.ndarray, n: int) -> np.ndarray:
    return _df(x).rolling(n, min_periods=_mp(n)).std(ddof=1).to_numpy()


def rolling_max(x: np.ndarray, n: int) -> np.ndarray:
    return _df(x).rolling(n, min_periods=_mp(n)).max().to_numpy()


def rolling_min(x: np.ndarray, n: int) -> np.ndarray:
    return _df(x).rolling(n, min_periods=_mp(n)).min().to_numpy()


def shift(x: np.ndarray, k: int = 1) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=float)
    if k > 0:
        out[k:] = x[:-k]
    elif k == 0:
        out[:] = x
    else:
        raise ValueError("negative shift would use future data")
    return out


def pct_change(x: np.ndarray, k: int = 1) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return x / shift(x, k) - 1.0


def log_returns(close: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.log(close / shift(close, 1))


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    pc = shift(close, 1)
    tr = np.fmax(high - low, np.fmax(np.abs(high - pc), np.abs(low - pc)))
    tr[np.isnan(high) | np.isnan(low)] = np.nan
    return tr


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray:
    """Wilder's ATR (recursive smoothing alpha = 1/n)."""
    return _df(true_range(high, low, close)).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().to_numpy()


def rsi(close: np.ndarray, n: int = 14) -> np.ndarray:
    """Wilder's RSI."""
    d = close - shift(close, 1)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    up[np.isnan(d)] = np.nan
    dn[np.isnan(d)] = np.nan
    au = _df(up).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().to_numpy()
    ad = _df(dn).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = au / ad
        out = 100.0 - 100.0 / (1.0 + rs)
    out[(ad == 0) & (au > 0)] = 100.0
    out[(ad == 0) & (au == 0)] = 50.0
    return out


def bollinger(close: np.ndarray, n: int = 20, k: float = 2.0):
    mid = sma(close, n)
    sd = rolling_std(close, n)
    return mid, mid + k * sd, mid - k * sd


def zscore(x: np.ndarray, n: int) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return (x - sma(x, n)) / rolling_std(x, n)


def realised_vol(close: np.ndarray, n: int = 21, annualise: bool = True) -> np.ndarray:
    v = rolling_std(log_returns(close), n)
    return v * np.sqrt(252) if annualise else v


def momentum(close: np.ndarray, lookback: int, skip: int = 0) -> np.ndarray:
    """Total return from t-lookback to t-skip (skip avoids short-term reversal)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return shift(close, skip) / shift(close, lookback) - 1.0


def expanding_percentile_rank(x: np.ndarray, min_periods: int = 252) -> np.ndarray:
    """Percentile rank of x[t] among x[0..t] (inclusive) - causal; 1-D input."""
    s = pd.Series(np.asarray(x, dtype=float))
    return s.expanding(min_periods=min_periods).rank(pct=True).to_numpy()


def cross_sectional_rank(x: np.ndarray) -> np.ndarray:
    """Rank across columns at each row (1 = highest). NaN stays NaN."""
    return pd.DataFrame(x).rank(axis=1, ascending=False, method="first").to_numpy()
