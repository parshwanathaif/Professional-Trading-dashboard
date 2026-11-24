"""Market-regime classification from the benchmark index (causal, rule-based).

Why rule-based rather than e.g. a fitted HMM: rules are transparent, have few parameters,
cannot be refit on future data, and their thresholds are expressed relative to the index's
OWN expanding history (percentiles), which keeps them stable across decades.

Primary regime (one label per day, by precedence):
  MARKET_STRESS   drawdown from 252d high >= stress_drawdown AND vol percentile >= vol_high_pct
  HIGH_VOLATILITY vol percentile >= vol_high_pct
  STRONG_BULLISH  close > SMA(trend) AND SMA(trend) rising AND close > SMA(slope)
  WEAK_BULLISH    close > SMA(trend) (other)
  STRONG_BEARISH  close < SMA(trend) AND SMA(trend) falling AND close < SMA(slope)
  WEAK_BEARISH    close < SMA(trend) (other)
  SIDEWAYS        |close/SMA(trend) - 1| < 2%  and |SMA slope| small
  LOW_VOLATILITY  is reported as a separate flag (vol percentile <= vol_low_pct)
Percentiles are EXPANDING (each day ranked only against its own past).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import expanding_percentile_rank

REGIMES = [
    "STRONG_BULLISH", "WEAK_BULLISH", "SIDEWAYS", "WEAK_BEARISH", "STRONG_BEARISH",
    "HIGH_VOLATILITY", "LOW_VOLATILITY", "MARKET_STRESS", "UNKNOWN",
]


def detect_regimes(benchmark: pd.Series, trend_window: int = 200, slope_window: int = 50,
                   vol_window: int = 21, vol_high_pct: float = 0.8, vol_low_pct: float = 0.2,
                   stress_drawdown: float = 0.15, min_history: int = 252) -> pd.DataFrame:
    c = benchmark.astype(float)
    sma_t = c.rolling(trend_window, min_periods=trend_window).mean()
    sma_s = c.rolling(slope_window, min_periods=slope_window).mean()
    slope = sma_t / sma_t.shift(20) - 1.0
    vol = np.log(c / c.shift(1)).rolling(vol_window, min_periods=vol_window).std() * np.sqrt(252)
    vol_pct = pd.Series(expanding_percentile_rank(vol.to_numpy(), min_periods=min_history), index=c.index)
    dd = c / c.rolling(252, min_periods=60).max() - 1.0
    dist = c / sma_t - 1.0

    label = pd.Series("UNKNOWN", index=c.index, dtype=object)
    valid = sma_t.notna() & vol_pct.notna() & slope.notna()
    up = valid & (c > sma_t)
    dn = valid & (c <= sma_t)
    label[up] = "WEAK_BULLISH"
    label[dn] = "WEAK_BEARISH"
    label[up & (slope > 0) & (c > sma_s)] = "STRONG_BULLISH"
    label[dn & (slope < 0) & (c < sma_s)] = "STRONG_BEARISH"
    label[valid & (dist.abs() < 0.02) & (slope.abs() < 0.01)] = "SIDEWAYS"
    label[valid & (vol_pct >= vol_high_pct)] = "HIGH_VOLATILITY"
    label[valid & (vol_pct >= vol_high_pct) & (dd <= -stress_drawdown)] = "MARKET_STRESS"
    return pd.DataFrame({
        "regime": label,
        "low_volatility": valid & (vol_pct <= vol_low_pct),
        "vol_ann": vol,
        "vol_pct": vol_pct,
        "drawdown": dd,
        "trend_dist": dist,
        "trend_slope": slope,
    })


def detect_from_cfg(benchmark: pd.Series, rcfg) -> pd.DataFrame:
    return detect_regimes(benchmark, rcfg.trend_window, rcfg.slope_window, rcfg.vol_window,
                          rcfg.vol_high_pct, rcfg.vol_low_pct, rcfg.stress_drawdown, rcfg.min_history)
