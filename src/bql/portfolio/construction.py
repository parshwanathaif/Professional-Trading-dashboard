"""Multi-strategy portfolio construction on strategy return streams (research tool).

Weights are re-estimated monthly using ONLY returns observed before the rebalance date
(causal). Methods:
  equal               w_i = 1/N
  inverse_volatility  w_i proportional to 1/sigma_i
  risk_parity         equal risk contribution (ERC), solved iteratively on the covariance matrix
A per-strategy weight cap is applied and weights renormalised.

This is a diversification analysis tool; the single-book multi-strategy backtest (shared
capital and shared risk limits) is produced by `run_backtest` with several strategies.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _erc(cov: np.ndarray, iters: int = 500, tol: float = 1e-10) -> np.ndarray:
    n = cov.shape[0]
    w = np.ones(n) / n
    for _ in range(iters):
        mrc = cov @ w
        rc = w * mrc
        target = rc.sum() / n
        w_new = w * (target / np.where(rc > 0, rc, target)) ** 0.5
        w_new = np.clip(w_new, 1e-8, None)
        w_new /= w_new.sum()
        if np.max(np.abs(w_new - w)) < tol:
            w = w_new
            break
        w = w_new
    return w


def _cap(w: np.ndarray, cap: float) -> np.ndarray:
    w = w / w.sum()
    if cap * len(w) < 1.0:
        return np.ones(len(w)) / len(w)
    for _ in range(50):
        over = w > cap
        if not over.any():
            break
        excess = (w[over] - cap).sum()
        w[over] = cap
        under = ~over
        w[under] += excess * w[under] / w[under].sum()
    return w


def combine_strategies(returns: pd.DataFrame, method: str = "inverse_volatility", lookback: int = 126,
                       max_weight: float = 0.4) -> tuple[pd.Series, pd.DataFrame]:
    r = returns.fillna(0.0)
    n = r.shape[1]
    months = r.index.to_period("M")
    rebal = np.r_[True, months[1:] != months[:-1]]
    weights = np.zeros_like(r.to_numpy())
    w = np.ones(n) / n
    for t in range(len(r)):
        if rebal[t] and t >= 20:
            hist = r.iloc[max(0, t - lookback):t].to_numpy()
            if method == "equal":
                w = np.ones(n) / n
            else:
                sd = hist.std(axis=0, ddof=1)
                sd = np.where(sd > 0, sd, np.nanmax(sd) if np.nanmax(sd) > 0 else 1.0)
                if method == "inverse_volatility":
                    w = 1 / sd
                elif method == "risk_parity":
                    cov = np.cov(hist.T) if n > 1 else np.array([[sd[0] ** 2]])
                    cov = cov + np.eye(n) * 1e-10
                    w = _erc(np.atleast_2d(cov))
                else:
                    raise ValueError(f"unknown method {method}")
            w = _cap(np.asarray(w, dtype=float), max_weight)
        weights[t] = w
    wdf = pd.DataFrame(weights, index=r.index, columns=r.columns)
    port = (r * wdf).sum(axis=1)
    return port, wdf


def diversification_report(returns: pd.DataFrame) -> dict:
    r = returns.dropna(how="all").fillna(0.0)
    if r.shape[1] < 2:
        return {}
    corr = r.corr()
    vols = r.std() * np.sqrt(252)
    ew = r.mean(axis=1)
    port_vol = ew.std() * np.sqrt(252)
    return {
        "correlation": corr.round(3).to_dict(),
        "avg_pairwise_correlation": float(corr.where(~np.eye(len(corr), dtype=bool)).stack().mean()),
        "diversification_ratio_equal_weight": float(vols.mean() / port_vol) if port_vol > 0 else np.nan,
    }
