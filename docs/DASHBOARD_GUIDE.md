# Dashboard Guide

```bash
streamlit run dashboard.py
```

To view another workspace, for example the synthetic demo:

```bash
BQL_ROOT=$PWD/demo_workspace streamlit run dashboard.py
```

The dashboard reads the market database (read-only) and the paper ledger (read-only SQLite URI). The only actions that write are **Run backtest** and **Generate report**. Both pass through the same data-quality gate as the CLI.

The header always shows the paper-only notice, a red banner if synthetic data is loaded, and the status of the last workflow, including any error.

| Section | What you see |
|---|---|
| Market overview | Benchmark level and change, current regime, realised volatility and its percentile, drawdown from the 1-year high, chart with stress regimes shaded, regime distribution, advances/declines and the cumulative A/D line, sector median returns over 1M, 3M and 12M, and other loaded indices (e.g. NIFTY 500) |
| Strategy research | Latest research run: summary (status, score, WF and test Sharpe, DSR, PBO), comparison scatter, then per strategy the hypothesis, logic, maths, status reasons, train/validation/test metrics (training labelled IN-SAMPLE), growth curves, walk-forward folds, robustness, statistics and weaknesses |
| Portfolio | Simulated equity, cash, invested amount and exposure, unrealised and realised P&L, drawdown, equity curve (net, with gross dotted and the benchmark), drawdown chart, open positions, sector allocation, and lifetime metrics |
| Risk management | Kill switch with reset instructions, suspended strategies, soft blocks, drawdown against limits, gross exposure, position count, sector weights, holdings correlation heatmap, configured limits, and the full risk-event log |
| Backtesting | Interactive single backtest (labelled in-sample) with equity curve, drawdown, monthly heatmap, yearly returns, rolling Sharpe and volatility, trade distribution and cost analysis |
| Trade journal | Results by strategy, closed trades filtered by strategy and outcome, fills, and every signal with its decision, reason, indicator values and sizing |
| Reports | Generate daily, weekly, monthly or annual reports; download PDF, Excel, HTML, Markdown or CSV; data provenance table |
