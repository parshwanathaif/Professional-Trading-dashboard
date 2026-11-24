# Project Plan & Status

| Phase | Deliverable | Status |
|---|---|---|
| 1. Research & architecture | MARKET_RESEARCH.md, REGULATORY_REFERENCE.md, SYSTEM_ARCHITECTURE.md, DATA_REQUIREMENTS.md, RISK_METHODOLOGY.md | Done |
| 2. Data infrastructure | CSV/Parquet import, provider interface, DuckDB store with provenance and quarantine, data-quality engine, corporate actions, calendar, point-in-time panel | Done |
| 3. Strategy library | 6 strategies (trend, momentum, 2× mean reversion, breakout, volatility) plus regime detection | Done |
| 4. Backtesting engine | Event-driven simulator: t+1 execution, costs, spread/impact, participation caps, circuit locks, stops, dividends, delistings | Done |
| 5. Statistical validation | Chronological split, walk-forward, robustness suite, PSR, DSR, bootstrap, Monte Carlo, PBO, leakage test, scoring and status | Done |
| 6. Risk engine | Sizing, exposure/sector/correlation/liquidity limits, loss limits, kill switch, suspension, regime gating | Done |
| 7. Paper trading | SQLite ledger, atomic idempotent sessions, audit trail, manual resets | Done |
| 8. Dashboard & automation | Streamlit (7 sections), daily/weekly/monthly workflows, locks, registry, daemon, reports (MD/HTML/CSV/XLSX/PDF) | Done |
| 9. Independent audit | Test suite, end-to-end demo, FINAL_AUDIT_REPORT.md | Done |
| 10. Operating instructions | README, INSTALLATION, PAPER_TRADING_MANUAL, TROUBLESHOOTING | Done |

## Assumptions made (documented rather than asked)
1. Long-only, delivery, cash equities. No leverage, shorting, intraday or derivatives.
2. Daily bars. Signal at the close, execution at the next session's open.
3. Default capital is ₹1 crore (simulated) and default brokerage is 0.03% uncapped. Both are configurable.
4. NSE is the primary exchange. BSE data can be imported, but BSE charges are placeholders.
5. No licensed data source is connected in this installation. The platform runs in offline CSV mode, and the shipped demo uses synthetic data that is clearly flagged as such.

## Next steps for the operator
1. Obtain licensed EOD data with corporate actions, point-in-time index membership and delisted securities (see DATA_REQUIREMENTS.md).
2. Load the official holiday lists. Set `data.universe_index`.
3. Verify the `verify`-flagged rows in `config/transaction_costs.yaml`.
4. Run `python main.py research`. Paper-trade only strategies that reach "Eligible for paper-trading evaluation".
