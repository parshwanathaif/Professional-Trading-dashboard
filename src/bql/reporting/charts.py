"""Plotly chart builders shared by the dashboard and HTML reports."""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from ..backtesting.metrics import drawdown_series, monthly_returns_table, rolling_sharpe, yearly_returns

NET = "#1f5fa8"
GROSS = "#9aa7b8"
BENCH = "#c2711b"
NEG = "#b23b3b"
POS = "#2f7d4f"


def _layout(fig: go.Figure, title: str, height: int = 360, yfmt: str | None = None) -> go.Figure:
    fig.update_layout(title=title, height=height, margin=dict(l=50, r=20, t=50, b=40), hovermode="x unified",
                      legend=dict(orientation="h", y=-0.15), template="plotly_white")
    if yfmt:
        fig.update_yaxes(tickformat=yfmt)
    return fig


def equity_curve(equity: pd.DataFrame, benchmark: pd.Series | None = None, title: str = "Equity curve") -> go.Figure:
    e = equity.set_index("date")
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=e.index, y=e["equity"], name="Net of simulated costs", line=dict(color=NET, width=2)))
    if "gross_equity" in e:
        fig.add_trace(go.Scatter(x=e.index, y=e["gross_equity"], name="Gross (before costs)",
                                 line=dict(color=GROSS, width=1, dash="dot")))
    if benchmark is not None:
        b = benchmark.reindex(e.index).dropna()
        if len(b):
            fig.add_trace(go.Scatter(x=b.index, y=b / b.iloc[0] * e["equity"].iloc[0], name="Benchmark (rebased)",
                                     line=dict(color=BENCH, width=1)))
    return _layout(fig, title, yfmt=",.0f")


def returns_curve(returns: dict[str, pd.Series], title: str = "Growth of 1") -> go.Figure:
    fig = go.Figure()
    for name, r in returns.items():
        if r is None or len(r) == 0:
            continue
        fig.add_trace(go.Scatter(x=r.index, y=(1 + r.fillna(0)).cumprod(), name=name))
    return _layout(fig, title)


def drawdown_chart(equity: pd.Series, title: str = "Drawdown") -> go.Figure:
    dd = drawdown_series(equity)
    fig = go.Figure(go.Scatter(x=dd.index, y=dd, fill="tozeroy", line=dict(color=NEG, width=1), name="Drawdown"))
    return _layout(fig, title, 260, ".0%")


def monthly_heatmap(equity: pd.Series, title: str = "Monthly returns (net)") -> go.Figure:
    tbl = monthly_returns_table(equity)
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    tbl = tbl.reindex(columns=range(1, 13))
    z = tbl.to_numpy()
    lim = np.nanmax(np.abs(z)) if np.isfinite(z).any() else 0.1
    fig = go.Figure(go.Heatmap(z=z, x=months, y=[str(y) for y in tbl.index], colorscale="RdYlGn", zmid=0,
                               zmin=-lim, zmax=lim, text=[[f"{v:.1%}" if np.isfinite(v) else "" for v in row] for row in z],
                               texttemplate="%{text}", hovertemplate="%{y} %{x}: %{z:.2%}<extra></extra>"))
    return _layout(fig, title, max(260, 24 * len(tbl) + 80))


def yearly_bars(equity: pd.Series, benchmark: pd.Series | None = None, title: str = "Calendar-year returns") -> go.Figure:
    y = yearly_returns(equity)
    fig = go.Figure(go.Bar(x=y.index.astype(str), y=y, name="Strategy (net)",
                           marker_color=[POS if v >= 0 else NEG for v in y]))
    if benchmark is not None:
        b = benchmark.reindex(equity.index).dropna()
        if len(b):
            yb = yearly_returns(b)
            fig.add_trace(go.Bar(x=yb.index.astype(str), y=yb, name="Benchmark", marker_color=BENCH, opacity=0.6))
    return _layout(fig, title, 300, ".0%")


def rolling_stats(returns: pd.Series, window: int = 126) -> go.Figure:
    rs = rolling_sharpe(returns, window)
    rv = returns.rolling(window).std() * np.sqrt(252)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=rs.index, y=rs, name=f"Rolling Sharpe ({window}d)", line=dict(color=NET)))
    fig.add_trace(go.Scatter(x=rv.index, y=rv, name=f"Rolling volatility ({window}d)", yaxis="y2",
                             line=dict(color=BENCH)))
    fig.update_layout(yaxis2=dict(overlaying="y", side="right", tickformat=".0%"))
    return _layout(fig, "Rolling Sharpe and volatility", 300)


def trade_distribution(trades: pd.DataFrame, title: str = "Trade return distribution (net)") -> go.Figure:
    fig = go.Figure()
    if trades is not None and not trades.empty:
        r = trades["return_pct"]
        fig.add_trace(go.Histogram(x=r[r > 0], name="Winners", marker_color=POS, opacity=0.75, nbinsx=40))
        fig.add_trace(go.Histogram(x=r[r <= 0], name="Losers", marker_color=NEG, opacity=0.75, nbinsx=40))
        fig.update_layout(barmode="overlay")
    return _layout(fig, title, 280, None).update_xaxes(tickformat=".0%")


def sector_allocation(positions: pd.DataFrame, title: str = "Sector allocation") -> go.Figure:
    if positions is None or positions.empty:
        return _layout(go.Figure(), title + " (no open positions)", 280)
    s = positions.groupby("sector")["market_value"].sum().sort_values()
    fig = go.Figure(go.Bar(x=s.values, y=s.index, orientation="h", marker_color=NET))
    return _layout(fig, title, 280)


def strategy_comparison(summary: pd.DataFrame, x: str = "wf_oos_sharpe", y: str = "test_mdd") -> go.Figure:
    fig = go.Figure()
    if summary is not None and not summary.empty:
        fig.add_trace(go.Scatter(x=summary[x], y=summary[y], mode="markers+text", text=summary["strategy"],
                                 textposition="top center", marker=dict(size=12, color=summary["score"],
                                                                        colorscale="Blues", showscale=True,
                                                                        colorbar=dict(title="Score"))))
    fig.update_xaxes(title="Walk-forward OOS Sharpe (excess of risk-free)")
    fig.update_yaxes(title="Test max drawdown", tickformat=".0%")
    return _layout(fig, "Strategy comparison (out-of-sample)", 380)


def correlation_heatmap(corr: pd.DataFrame, title: str = "Correlation") -> go.Figure:
    fig = go.Figure(go.Heatmap(z=corr.to_numpy(), x=list(corr.columns), y=list(corr.index), colorscale="RdBu",
                               zmid=0, zmin=-1, zmax=1, texttemplate="%{z:.2f}"))
    return _layout(fig, title, 360)
