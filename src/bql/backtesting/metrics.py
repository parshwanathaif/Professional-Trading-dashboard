"""Performance metrics. Net (after simulated costs) figures are primary; gross shown separately."""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def _ret(equity: pd.Series) -> pd.Series:
    return equity.pct_change().dropna()


def cagr(equity: pd.Series) -> float:
    if len(equity) < 2 or equity.iloc[0] <= 0:
        return float("nan")
    years = len(equity) / TRADING_DAYS
    if equity.iloc[-1] <= 0:
        return -1.0
    return float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1)


def sharpe(returns: pd.Series, rf_annual: float = 0.0) -> float:
    r = returns.dropna() - rf_annual / TRADING_DAYS
    if len(r) < 2 or r.std(ddof=1) == 0:
        return float("nan")
    return float(r.mean() / r.std(ddof=1) * np.sqrt(TRADING_DAYS))


def sortino(returns: pd.Series, rf_annual: float = 0.0) -> float:
    r = returns.dropna() - rf_annual / TRADING_DAYS
    downside = np.sqrt(np.mean(np.minimum(r, 0) ** 2))
    if len(r) < 2 or downside == 0:
        return float("nan")
    return float(r.mean() / downside * np.sqrt(TRADING_DAYS))


def drawdown_series(equity: pd.Series) -> pd.Series:
    return equity / equity.cummax() - 1.0


def max_drawdown(equity: pd.Series) -> float:
    return float(drawdown_series(equity).min()) if len(equity) else float("nan")


def recovery_days(equity: pd.Series) -> tuple[int | None, bool]:
    """Sessions from the max-drawdown trough back to the prior peak (None if not recovered)."""
    dd = drawdown_series(equity)
    if dd.empty or dd.min() == 0:
        return 0, True
    trough = int(np.argmin(dd.to_numpy()))
    peak_val = equity.iloc[: trough + 1].max()
    after = np.flatnonzero(equity.iloc[trough:].to_numpy() >= peak_val)
    if len(after) == 0:
        return None, False
    return int(after[0]), True


def cvar(returns: pd.Series, alpha: float = 0.05) -> float:
    r = returns.dropna()
    if len(r) < 20:
        return float("nan")
    q = r.quantile(alpha)
    return float(r[r <= q].mean())


def var(returns: pd.Series, alpha: float = 0.05) -> float:
    r = returns.dropna()
    return float(r.quantile(alpha)) if len(r) >= 20 else float("nan")


def trade_stats(trades: pd.DataFrame) -> dict:
    if trades is None or trades.empty:
        return {"trades": 0, "win_rate": np.nan, "avg_win": np.nan, "avg_loss": np.nan, "profit_factor": np.nan,
                "payoff_ratio": np.nan, "avg_holding_days": np.nan, "expectancy_inr": np.nan,
                "max_consecutive_losses": 0, "avg_trade_return": np.nan}
    pnl = trades["net_pnl"]
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gp, gl = wins.sum(), -losses.sum()
    streak = mx = 0
    for v in pnl:
        streak = streak + 1 if v <= 0 else 0
        mx = max(mx, streak)
    return {
        "trades": int(len(trades)),
        "win_rate": float(len(wins) / len(trades)),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "profit_factor": float(gp / gl) if gl > 0 else float("inf") if gp > 0 else float("nan"),
        "payoff_ratio": float(wins.mean() / -losses.mean()) if len(wins) and len(losses) and losses.mean() < 0 else float("nan"),
        "avg_holding_days": float(trades["holding_days"].mean()),
        "expectancy_inr": float(pnl.mean()),
        "max_consecutive_losses": int(mx),
        "avg_trade_return": float(trades["return_pct"].mean()),
    }


def compute_metrics(equity_df: pd.DataFrame, trades: pd.DataFrame, benchmark: pd.Series | None = None,
                    rf_annual: float = 0.065, turnover_inr: float | None = None) -> dict:
    """Full metric set. `equity_df` needs columns date, equity, gross_equity, exposure, cum_charges, cum_exec_cost."""
    if equity_df is None or equity_df.empty:
        return {}
    e = equity_df.set_index("date")["equity"].astype(float)
    g = equity_df.set_index("date")["gross_equity"].astype(float)
    r = _ret(e)
    rg = _ret(g)
    years = len(e) / TRADING_DAYS
    rec, recovered = recovery_days(e)
    out = {
        "start": str(pd.Timestamp(e.index[0]).date()),
        "end": str(pd.Timestamp(e.index[-1]).date()),
        "years": round(years, 2),
        "initial_capital": float(e.iloc[0]),
        "final_capital_net": float(e.iloc[-1]),
        "abs_pnl_net": float(e.iloc[-1] - e.iloc[0]),
        "total_return_net": float(e.iloc[-1] / e.iloc[0] - 1),
        "cagr_net": cagr(e),
        "annualised_return_net": float(r.mean() * TRADING_DAYS) if len(r) else np.nan,
        "final_capital_gross": float(g.iloc[-1]),
        "cagr_gross": cagr(g),
        "sharpe_net": sharpe(r, rf_annual),
        "sharpe_gross": sharpe(rg, rf_annual),
        "sortino_net": sortino(r, rf_annual),
        "volatility_ann": float(r.std(ddof=1) * np.sqrt(TRADING_DAYS)) if len(r) > 1 else np.nan,
        "max_drawdown": max_drawdown(e),
        "recovery_sessions": rec,
        "recovered": recovered,
        "var_95_daily": var(r),
        "cvar_95_daily": cvar(r),
        "market_exposure_avg": float(equity_df["exposure"].mean()),
        "total_charges_inr": float(equity_df["cum_charges"].iloc[-1]),
        "total_exec_cost_inr": float(equity_df["cum_exec_cost"].iloc[-1]),
    }
    md = out["max_drawdown"]
    out["calmar"] = float(out["cagr_net"] / abs(md)) if md and md < 0 and np.isfinite(out["cagr_net"]) else np.nan
    avg_eq = float(e.mean())
    if turnover_inr is not None and years > 0:
        out["annual_turnover"] = float(turnover_inr / avg_eq / years)
    out["cost_drag_annual"] = (out["cagr_gross"] - out["cagr_net"]) if np.isfinite(out["cagr_gross"]) else np.nan
    out.update(trade_stats(trades))
    out["risk_reward_ratio"] = out.get("payoff_ratio")
    if benchmark is not None and len(benchmark):
        b = benchmark.reindex(e.index).dropna()
        if len(b) > 2:
            br = _ret(b)
            aligned = pd.concat([r, br], axis=1, join="inner").dropna()
            out["benchmark_cagr"] = cagr(b)
            out["benchmark_sharpe"] = sharpe(br, rf_annual)
            out["benchmark_max_drawdown"] = max_drawdown(b)
            if len(aligned) > 20 and aligned.iloc[:, 1].var() > 0:
                cov = np.cov(aligned.iloc[:, 0], aligned.iloc[:, 1])
                beta = cov[0, 1] / cov[1, 1]
                out["beta"] = float(beta)
                active = aligned.iloc[:, 0] - aligned.iloc[:, 1]
                out["information_ratio"] = float(active.mean() / active.std(ddof=1) * np.sqrt(TRADING_DAYS)) \
                    if active.std(ddof=1) > 0 else np.nan
                out["excess_cagr_vs_benchmark"] = out["cagr_net"] - out["benchmark_cagr"]
    return out


def monthly_returns_table(equity: pd.Series) -> pd.DataFrame:
    m = equity.resample("ME").last().pct_change()
    first = equity.resample("ME").last()
    if len(first):
        m.iloc[0] = first.iloc[0] / equity.iloc[0] - 1
    df = pd.DataFrame({"year": m.index.year, "month": m.index.month, "ret": m.values})
    return df.pivot(index="year", columns="month", values="ret")


def yearly_returns(equity: pd.Series) -> pd.Series:
    y = equity.resample("YE").last()
    out = y.pct_change()
    if len(y):
        out.iloc[0] = y.iloc[0] / equity.iloc[0] - 1
    out.index = out.index.year
    return out


def rolling_sharpe(returns: pd.Series, window: int = 126) -> pd.Series:
    m = returns.rolling(window).mean()
    s = returns.rolling(window).std()
    return m / s * np.sqrt(TRADING_DAYS)
