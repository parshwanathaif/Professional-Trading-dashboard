"""Statistical validation tools.

References
----------
* Bailey, D. & Lopez de Prado, M. (2012). The Sharpe Ratio Efficient Frontier. J. of Risk.
  (Probabilistic Sharpe Ratio)
* Bailey, D. & Lopez de Prado, M. (2014). The Deflated Sharpe Ratio: Correcting for Selection
  Bias, Backtest Overfitting and Non-Normality. J. of Portfolio Management 40(5), 94-107.
* Bailey, D., Borwein, J., Lopez de Prado, M. & Zhu, Q. (2017). The Probability of Backtest
  Overfitting. J. of Computational Finance. (CSCV / PBO)
* Politis, D. & Romano, J. (1994). The Stationary Bootstrap. JASA.
"""

from __future__ import annotations

from itertools import combinations

import numpy as np
import pandas as pd
from scipy import stats as sps

EULER_GAMMA = 0.5772156649015329


def _clean(r) -> np.ndarray:
    a = np.asarray(r, dtype=float)
    return a[np.isfinite(a)]


def sharpe_per_period(r) -> float:
    a = _clean(r)
    if len(a) < 2 or a.std(ddof=1) == 0:
        return float("nan")
    return float(a.mean() / a.std(ddof=1))


def probabilistic_sharpe_ratio(r, sr_benchmark: float = 0.0) -> float:
    """P(true SR > sr_benchmark); SRs in per-period (non-annualised) units."""
    a = _clean(r)
    n = len(a)
    if n < 10:
        return float("nan")
    sr = sharpe_per_period(a)
    if not np.isfinite(sr):
        return float("nan")
    g3 = float(sps.skew(a, bias=False))
    g4 = float(sps.kurtosis(a, fisher=False, bias=False))
    denom = 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr**2
    if denom <= 0:
        return float("nan")
    z = (sr - sr_benchmark) * np.sqrt(n - 1) / np.sqrt(denom)
    return float(sps.norm.cdf(z))


def expected_max_sharpe(trial_srs, n_trials: int | None = None) -> float:
    """Expected maximum SR under the null of zero skill given N trials (per-period units)."""
    s = _clean(trial_srs)
    N = n_trials or len(s)
    if N < 2 or len(s) < 2:
        return 0.0
    var = float(np.var(s, ddof=1))
    return float(np.sqrt(var) * ((1 - EULER_GAMMA) * sps.norm.ppf(1 - 1.0 / N)
                                 + EULER_GAMMA * sps.norm.ppf(1 - 1.0 / (N * np.e))))


def deflated_sharpe_ratio(r, trial_srs, n_trials: int | None = None) -> tuple[float, float]:
    """Return (DSR probability, SR0 threshold per period)."""
    sr0 = expected_max_sharpe(trial_srs, n_trials)
    return probabilistic_sharpe_ratio(r, sr0), sr0


def stationary_bootstrap_indices(n: int, mean_block: int, rng: np.random.Generator) -> np.ndarray:
    idx = np.empty(n, dtype=int)
    p = 1.0 / max(mean_block, 1)
    idx[0] = rng.integers(n)
    for i in range(1, n):
        idx[i] = rng.integers(n) if rng.random() < p else (idx[i - 1] + 1) % n
    return idx


def bootstrap_sharpe(r, samples: int = 1000, block: int = 20, seed: int = 0) -> dict:
    a = _clean(r)
    if len(a) < 60:
        return {"n": len(a), "ci_low": np.nan, "ci_high": np.nan, "median": np.nan, "p_positive": np.nan}
    rng = np.random.default_rng(seed)
    out = np.empty(samples)
    for k in range(samples):
        s = a[stationary_bootstrap_indices(len(a), block, rng)]
        sd = s.std(ddof=1)
        out[k] = s.mean() / sd * np.sqrt(252) if sd > 0 else 0.0
    return {"n": int(len(a)), "ci_low": float(np.percentile(out, 5)), "median": float(np.median(out)),
            "ci_high": float(np.percentile(out, 95)), "p_positive": float((out > 0).mean())}


def monte_carlo_paths(r, paths: int = 1000, horizon: int = 252, block: int = 20, seed: int = 0) -> dict:
    """Block-bootstrap 1-year paths: distribution of return and max drawdown."""
    a = _clean(r)
    if len(a) < 60:
        return {"p_loss_1y": np.nan, "median_return_1y": np.nan, "mdd_p50": np.nan, "mdd_p95": np.nan}
    rng = np.random.default_rng(seed)
    rets, mdds = np.empty(paths), np.empty(paths)
    for k in range(paths):
        s = a[stationary_bootstrap_indices(len(a), block, rng)[:horizon]] if len(a) >= horizon else \
            a[rng.integers(len(a), size=horizon)]
        eq = np.cumprod(1 + s)
        rets[k] = eq[-1] - 1
        mdds[k] = (eq / np.maximum.accumulate(eq) - 1).min()
    return {"p_loss_1y": float((rets < 0).mean()), "median_return_1y": float(np.median(rets)),
            "return_p05_1y": float(np.percentile(rets, 5)), "mdd_p50": float(np.median(mdds)),
            "mdd_p95": float(np.percentile(mdds, 5))}


def trade_order_monte_carlo(trade_pnls, paths: int = 1000, seed: int = 0) -> dict:
    """Shuffle trade order: distribution of worst losing streak and peak-to-trough P&L."""
    a = _clean(trade_pnls)
    if len(a) < 10:
        return {"max_losing_streak_p95": np.nan, "pnl_drawdown_p95_inr": np.nan}
    rng = np.random.default_rng(seed)
    streaks, dds = np.empty(paths), np.empty(paths)
    for k in range(paths):
        s = rng.permutation(a)
        run = best = 0
        for v in s:
            run = run + 1 if v <= 0 else 0
            best = max(best, run)
        streaks[k] = best
        cum = np.cumsum(s)
        dds[k] = (cum - np.maximum.accumulate(np.r_[0, cum])[1:]).min()
    return {"max_losing_streak_p50": float(np.median(streaks)), "max_losing_streak_p95": float(np.percentile(streaks, 95)),
            "pnl_drawdown_p50_inr": float(np.median(dds)), "pnl_drawdown_p95_inr": float(np.percentile(dds, 5))}


def probability_of_backtest_overfitting(returns: pd.DataFrame, n_partitions: int = 10) -> dict:
    """CSCV estimate of PBO. `returns`: T x N matrix of daily returns, one column per configuration."""
    M = returns.dropna(how="all").fillna(0.0).to_numpy()
    T, N = M.shape
    if N < 2 or T < n_partitions * 20:
        return {"pbo": np.nan, "n_combinations": 0, "logits": []}
    S = n_partitions - (n_partitions % 2)
    blocks = np.array_split(np.arange(T), S)
    logits = []
    degradation = []
    for is_idx in combinations(range(S), S // 2):
        oos_idx = [b for b in range(S) if b not in is_idx]
        IS = M[np.concatenate([blocks[b] for b in is_idx])]
        OS = M[np.concatenate([blocks[b] for b in oos_idx])]
        sr_is = IS.mean(0) / np.where(IS.std(0, ddof=1) > 0, IS.std(0, ddof=1), np.inf)
        sr_os = OS.mean(0) / np.where(OS.std(0, ddof=1) > 0, OS.std(0, ddof=1), np.inf)
        best = int(np.argmax(sr_is))
        rank = sps.rankdata(sr_os)[best]          # 1 = worst
        w = rank / (N + 1)
        logits.append(float(np.log(w / (1 - w))))
        degradation.append((float(sr_is[best]), float(sr_os[best])))
    logits = np.array(logits)
    deg = np.array(degradation)
    return {"pbo": float((logits <= 0).mean()), "n_combinations": int(len(logits)),
            "median_logit": float(np.median(logits)),
            "is_sharpe_mean_ann": float(deg[:, 0].mean() * np.sqrt(252)),
            "oos_sharpe_of_is_best_mean_ann": float(deg[:, 1].mean() * np.sqrt(252))}


def parameter_neighbourhood(grid_results: pd.DataFrame, metric: str = "sharpe") -> dict:
    """How much of the parameter grid performs acceptably (plateau vs isolated peak)."""
    v = grid_results[metric].replace([np.inf, -np.inf], np.nan).dropna()
    if v.empty:
        return {"positive_fraction": np.nan, "dispersion": np.nan, "best_minus_median": np.nan}
    return {"positive_fraction": float((v > 0).mean()), "dispersion": float(v.std(ddof=0)),
            "best": float(v.max()), "median": float(v.median()), "best_minus_median": float(v.max() - v.median())}


def return_distribution(r) -> dict:
    a = _clean(r)
    if len(a) < 10:
        return {}
    jb = sps.jarque_bera(a)
    return {"skew": float(sps.skew(a)), "excess_kurtosis": float(sps.kurtosis(a)),
            "jarque_bera_p": float(jb.pvalue), "t_stat_mean": float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a))))}
