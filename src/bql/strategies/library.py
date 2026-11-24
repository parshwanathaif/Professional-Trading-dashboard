"""Strategy library. Each class documents hypothesis, plain-English logic and mathematics."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..data.panel import Panel
from ..indicators import (
    atr,
    bollinger,
    momentum,
    realised_vol,
    rolling_max,
    rolling_min,
    rsi,
    shift,
    sma,
    true_range,
)
from .base import Signals, Strategy


def _nan_false(a: np.ndarray) -> np.ndarray:
    return np.nan_to_num(a, nan=0.0).astype(bool)


class TrendMA(Strategy):
    name = "trend_ma"
    family = "Trend Following"
    hypothesis = ("Prices that trade above a rising long-term average tend to continue trending "
                  "(time-series momentum; behavioural under-reaction and herding).")
    description = ("Buy a stock when its fast moving average is above its slow moving average and the "
                   "price is above the fast average. Sell when the fast average falls below the slow "
                   "average or the price closes below the slow average. Candidates are ranked by "
                   "volatility-adjusted distance from the slow average.")
    mathematics = ("entry_t = 1[SMA_f(P)_t > SMA_s(P)_t  AND  P_t > SMA_f(P)_t];  "
                   "exit_t = 1[SMA_f(P)_t < SMA_s(P)_t  OR  P_t < SMA_s(P)_t];  "
                   "score_t = (P_t / SMA_s(P)_t - 1) / sigma_63,t")
    default_params = {"fast": 50, "slow": 200, "atr_window": 20}

    def _generate(self, panel: Panel, universe: np.ndarray) -> Signals:
        f, s = int(self.params["fast"]), int(self.params["slow"])
        if f >= s:
            f = max(5, s // 3)
        c = panel.close
        fma, sma_ = sma(c, f), sma(c, s)
        vol = realised_vol(c, 63, annualise=False)
        with np.errstate(invalid="ignore", divide="ignore"):
            entry = (fma > sma_) & (c > fma)
            exit_ = (fma < sma_) | (c < sma_)
            score = (c / sma_ - 1.0) / vol
        return Signals(entry=_nan_false(entry), exit=_nan_false(exit_), score=score,
                       stop_distance=self.default_stop(panel),
                       info={"sma_fast": fma, "sma_slow": sma_},
                       reasons={"entry": f"SMA{f} > SMA{s} and close > SMA{f}",
                                "exit": f"SMA{f} < SMA{s} or close < SMA{s}"})


class CrossSectionalMomentum(Strategy):
    name = "xs_momentum"
    family = "Momentum"
    hypothesis = ("Stocks with the strongest risk-adjusted 6-12 month returns (skipping the most recent "
                  "month) continue to outperform over the next months (Jegadeesh-Titman momentum).")
    description = ("Every `rebalance_days` sessions, rank eligible stocks by their return from 12 months "
                   "ago to 1 month ago divided by their volatility. Hold the top N stocks that also have "
                   "positive momentum. A holding is sold at a rebalance if its rank drops below "
                   "N x buffer or its momentum turns negative.")
    mathematics = ("MOM_t = P_{t-skip} / P_{t-L} - 1;  score_t = MOM_t / sigma_t (if vol_adjust);  "
                   "rank_t = cross-sectional rank of score among eligible stocks;  "
                   "entry_t = 1[rebalance_t AND rank_t <= N AND MOM_t > 0];  "
                   "exit_t = 1[rebalance_t AND (rank_t > buffer*N OR MOM_t <= 0)]")
    default_params = {"lookback": 252, "skip": 21, "top_n": 10, "rebalance_days": 21,
                      "vol_adjust": True, "exit_rank_buffer": 2.0}

    def _generate(self, panel: Panel, universe: np.ndarray) -> Signals:
        L, k = int(self.params["lookback"]), int(self.params["skip"])
        n = int(self.params["top_n"])
        mom = momentum(panel.close, L, k)
        score = mom.copy()
        if self.params.get("vol_adjust", True):
            vol = realised_vol(panel.close, 126, annualise=True)
            with np.errstate(invalid="ignore", divide="ignore"):
                score = mom / vol
        score = np.where(universe, score, np.nan)
        rank = pd.DataFrame(score).rank(axis=1, ascending=False, method="first").to_numpy()
        T = panel.T
        reb = np.zeros(T, dtype=bool)
        reb[:: int(self.params["rebalance_days"])] = True
        reb_col = reb[:, None]
        with np.errstate(invalid="ignore"):
            entry = reb_col & (rank <= n) & (mom > 0)
            exit_ = reb_col & ((~np.isfinite(rank)) | (rank > n * float(self.params["exit_rank_buffer"]))
                               | ~(mom > 0))
        return Signals(entry=_nan_false(entry), exit=_nan_false(exit_), score=score,
                       stop_distance=self.default_stop(panel), info={"momentum": mom, "rank": rank},
                       reasons={"entry": f"top-{n} risk-adjusted {L}/{k} momentum at rebalance",
                                "exit": "rank fell below buffer or momentum <= 0 at rebalance"})


class RSIMeanReversion(Strategy):
    name = "rsi_mean_reversion"
    family = "Mean Reversion"
    hypothesis = ("Short-term oversold moves in stocks that are in a long-term uptrend tend to partially "
                  "reverse within days (liquidity provision / overreaction).")
    description = ("Buy when a short-period RSI falls below an oversold threshold while the price is above "
                   "its long-term average. Sell when RSI recovers above the exit level or after max_hold days.")
    mathematics = ("RSI_n via Wilder smoothing;  entry_t = 1[RSI_n,t < a AND P_t > SMA_200,t];  "
                   "exit_t = 1[RSI_n,t > b];  score_t = -RSI_n,t")
    default_params = {"rsi_window": 3, "entry_rsi": 10, "exit_rsi": 60, "trend_window": 200, "max_hold": 10}

    def _generate(self, panel: Panel, universe: np.ndarray) -> Signals:
        r = rsi(panel.close, int(self.params["rsi_window"]))
        tr = sma(panel.close, int(self.params["trend_window"]))
        with np.errstate(invalid="ignore"):
            entry = (r < self.params["entry_rsi"]) & (panel.close > tr)
            exit_ = r > self.params["exit_rsi"]
        return Signals(entry=_nan_false(entry), exit=_nan_false(exit_), score=-r,
                       stop_distance=self.default_stop(panel), max_hold=int(self.params["max_hold"]),
                       info={"rsi": r, "sma_trend": tr},
                       reasons={"entry": f"RSI{self.params['rsi_window']} < {self.params['entry_rsi']} in uptrend",
                                "exit": f"RSI > {self.params['exit_rsi']}"})


class BollingerReversion(Strategy):
    name = "bollinger_reversion"
    family = "Mean Reversion"
    hypothesis = "Closes below the lower Bollinger band in an uptrend revert towards the moving average."
    description = ("Buy when the close is below the lower Bollinger band while above the long-term trend "
                   "average; sell when the close returns above the middle band or after max_hold days.")
    mathematics = ("mid = SMA_n(P), lower = mid - k*sd_n(P);  entry_t = 1[P_t < lower_t AND P_t > SMA_trend];  "
                   "exit_t = 1[P_t > mid_t];  score_t = (mid_t - P_t)/sd_t")
    default_params = {"window": 20, "num_std": 2.0, "trend_window": 200, "max_hold": 15}

    def _generate(self, panel: Panel, universe: np.ndarray) -> Signals:
        n, k = int(self.params["window"]), float(self.params["num_std"])
        mid, _, lower = bollinger(panel.close, n, k)
        sd = (mid - lower) / k
        tr = sma(panel.close, int(self.params["trend_window"]))
        with np.errstate(invalid="ignore", divide="ignore"):
            entry = (panel.close < lower) & (panel.close > tr)
            exit_ = panel.close > mid
            score = (mid - panel.close) / sd
        return Signals(entry=_nan_false(entry), exit=_nan_false(exit_), score=score,
                       stop_distance=self.default_stop(panel), max_hold=int(self.params["max_hold"]),
                       info={"bb_mid": mid, "bb_lower": lower},
                       reasons={"entry": f"close < BB({n},{k}) lower band in uptrend", "exit": "close > middle band"})


class DonchianBreakout(Strategy):
    name = "donchian_breakout"
    family = "Breakout"
    hypothesis = ("A close above the prior N-day high on above-average volume indicates new demand that "
                  "persists (breakout with participation).")
    description = ("Buy when today's close exceeds the highest high of the previous `entry_window` days and "
                   "today's volume exceeds `volume_mult` x its prior average. Sell when the close falls "
                   "below the lowest low of the previous `exit_window` days.")
    mathematics = ("HH_t = max(H_{t-N..t-1});  LL_t = min(L_{t-M..t-1});  "
                   "entry_t = 1[P_t > HH_t AND V_t > m * SMA_v(V)_{t-1}];  exit_t = 1[P_t < LL_t];  "
                   "score_t = (P_t - HH_t)/ATR_t")
    default_params = {"entry_window": 55, "exit_window": 20, "volume_mult": 1.5, "volume_window": 50,
                      "squeeze_pct": 1.0}

    def _generate(self, panel: Panel, universe: np.ndarray) -> Signals:
        hh = shift(rolling_max(panel.high, int(self.params["entry_window"])), 1)
        ll = shift(rolling_min(panel.low, int(self.params["exit_window"])), 1)
        avgv = shift(sma(panel.volume, int(self.params["volume_window"])), 1)
        a = atr(panel.high, panel.low, panel.close, 20)
        with np.errstate(invalid="ignore", divide="ignore"):
            entry = (panel.close > hh) & (panel.volume > float(self.params["volume_mult"]) * avgv)
            exit_ = panel.close < ll
            score = (panel.close - hh) / a
        return Signals(entry=_nan_false(entry), exit=_nan_false(exit_), score=score,
                       stop_distance=self.default_stop(panel), info={"prior_high": hh, "prior_low": ll},
                       reasons={"entry": f"close > {self.params['entry_window']}d high on volume",
                                "exit": f"close < {self.params['exit_window']}d low"})


class VolatilityBreakout(Strategy):
    name = "vol_breakout"
    family = "Volatility"
    hypothesis = ("Periods of unusually low volatility (compression) are followed by volatility expansion; "
                  "an upside range expansion in an uptrend marks the start of a directional move.")
    description = ("A stock qualifies when its normalised ATR was in the lowest `squeeze_pct` of its past "
                   "`squeeze_window` days within the last 5 sessions. Buy when today's true range exceeds "
                   "1.5x the prior ATR, the close is above the prior 20-day high and above the trend average. "
                   "Sell on a close below the prior `exit_window`-day low.")
    mathematics = ("NATR_t = ATR_t/P_t;  pct_t = rolling percentile of NATR over W;  "
                   "squeeze_t = min(pct_{t-5..t-1}) <= q;  entry_t = squeeze_t AND TR_t > 1.5 ATR_{t-1} "
                   "AND P_t > max(H_{t-20..t-1}) AND P_t > SMA_trend;  exit_t = P_t < min(L_{t-M..t-1})")
    default_params = {"atr_window": 14, "squeeze_window": 120, "squeeze_pct": 0.25, "trend_window": 100,
                      "exit_window": 20}

    def _generate(self, panel: Panel, universe: np.ndarray) -> Signals:
        a = atr(panel.high, panel.low, panel.close, int(self.params["atr_window"]))
        with np.errstate(invalid="ignore", divide="ignore"):
            natr = a / panel.close
        W = int(self.params["squeeze_window"])
        pct = pd.DataFrame(natr).rolling(W, min_periods=W).rank(pct=True).to_numpy()
        recent_min = shift(rolling_min(pct, 5), 1)
        tr = true_range(panel.high, panel.low, panel.close)
        hh = shift(rolling_max(panel.high, 20), 1)
        ll = shift(rolling_min(panel.low, int(self.params["exit_window"])), 1)
        trend = sma(panel.close, int(self.params["trend_window"]))
        prev_atr = shift(a, 1)
        with np.errstate(invalid="ignore", divide="ignore"):
            entry = ((recent_min <= float(self.params["squeeze_pct"])) & (tr > 1.5 * prev_atr)
                     & (panel.close > hh) & (panel.close > trend))
            exit_ = panel.close < ll
            score = tr / prev_atr
        return Signals(entry=_nan_false(entry), exit=_nan_false(exit_), score=score,
                       stop_distance=self.default_stop(panel), info={"natr_pct": pct, "atr": a},
                       reasons={"entry": "volatility squeeze then upside range expansion",
                                "exit": f"close < {self.params['exit_window']}d low"})


STRATEGIES: dict[str, type[Strategy]] = {
    c.name: c for c in (TrendMA, CrossSectionalMomentum, RSIMeanReversion, BollingerReversion,
                        DonchianBreakout, VolatilityBreakout)
}


def make_strategy(name: str, params: dict | None = None, atr_mult: float = 2.5) -> Strategy:
    if name not in STRATEGIES:
        raise KeyError(f"unknown strategy '{name}'. Available: {sorted(STRATEGIES)}")
    return STRATEGIES[name](params, atr_mult=atr_mult)
