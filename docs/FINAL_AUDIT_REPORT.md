# Final System Audit Report

**Date:** 2026-11-23 (IST) · **Version:** 0.1.0 · **Scope:** Phases 1–10

> **Headline.** The platform is complete and works end to end in offline mode. **No real market data is connected, so no strategy has been validated on Indian market data.** All results below come from synthetic software-test data with no embedded edge. On that data **all six strategies were rejected**, which is what a sound validation framework should do with noise.

## 1. What has been built
| Component | Location | Status |
|---|---|---|
| Config (Pydantic-validated YAML, SHA-256 hash on every run) | `config/`, `src/bql/config.py` | Working |
| Data import (CSV/Parquet), provider interface, licensed-vendor template | `src/bql/data/providers.py` | Working (vendor template intentionally unimplemented) |
| DuckDB store: provenance, idempotent re-import, quarantine of conflicts | `src/bql/data/store.py` | Working |
| Data-quality engine (block / exclude / warn; HTML, JSON and CSV report) | `src/bql/data/validation.py` | Working |
| Corporate actions (split, bonus, rights TERP, dividend cash, symbol-change chains, demerger factor) | `src/bql/data/corporate_actions.py` | Working |
| Point-in-time panel: universe, membership, price bands, raw vs adjusted prices | `src/bql/data/panel.py` | Working |
| NSE/BSE trading calendar (holidays, weekends, special sessions, IST close) | `src/bql/market_calendar/` | Working (official holiday files must be imported) |
| Causal indicators; regime classifier | `src/bql/indicators/`, `src/bql/regime/` | Working |
| Six strategies across five families | `src/bql/strategies/library.py` | Working |
| Date-effective Indian cost model plus spread, slippage and impact | `src/bql/costs/model.py` | Working |
| Independent risk engine | `src/bql/risk/engine.py` | Working |
| Event-driven simulator, backtest runner, metrics | `src/bql/backtesting/` | Working |
| Validation: splits, walk-forward, leakage, robustness, PSR/DSR/bootstrap/MC/PBO, scoring, status | `src/bql/validation/` | Working |
| Multi-strategy portfolio construction | `src/bql/portfolio/` | Working |
| Paper trading: SQLite ledger, atomic idempotent sessions, audit trail, manual resets | `src/bql/paper_trading/` | Working |
| Reports: strategy (12 sections), daily/weekly/monthly/annual, CSV/Excel/PDF/HTML | `src/bql/reporting/` | Working |
| Scheduler: locks, registry, resume, daemon, cron recipes | `src/bql/scheduler/` | Working |
| Streamlit dashboard (7 sections) | `dashboard/app.py` | Working (all sections exercised headless; 0 exceptions) |
| CLI | `main.py` | Working |

## 2. Verification performed
- **Automated tests:** **80 tests, 80 passed, 0 failed** (`python -m pytest`). Lint: `ruff check` reports no issues. One test failure during development turned out to be a wrong expectation in the test, which was corrected. Code was changed for three real defects found during the audit: an import column-order bug, an empty price-band file crash, and HTML reports that referenced a Plotly bundle that was never written.
- **End-to-end demo** (`python main.py demo --fresh`) on synthetic data covering 17 years and 60 symbols, with splits, bonus issues, a rights issue, 100+ dividends, a symbol change, two delistings, point-in-time membership, price bands with locked circuit days and a 120-bar data gap:
  - history imported, data-quality gate passed, and the gap symbol flagged;
  - full research run for 6 strategies (54 parameter trials) with reports in MD, HTML and PDF;
  - 250-session paper backfill, then **15 daily workflows fed through the inbox**, each re-run immediately to confirm duplicate protection (all `SKIPPED_ALREADY_COMPLETED`);
  - weekly review and monthly reassessment, including a second research run and a parameter-stability comparison.
  - Ledger after the demo: 266 processed sessions, 1,997 audited signals, 307 order events, 204 fills, 93 closed trades, and risk events: 2 daily-loss blocks, 1 gap alert, 1 strategy suspension.
- **Dashboard:** every section was opened in headless Chromium and an interactive backtest was run. No exceptions.

### Test coverage map
| Requirement | Tests |
|---|---|
| Indicators | `test_indicators.py` (8) |
| Signals / look-ahead leakage | causality test for every strategy, plus a deliberately leaky strategy that must be caught |
| Position sizing, risk limits, kill switch, suspension, regime gating, correlation | `test_risk.py` (11) |
| Portfolio accounting, cash ≥ 0, equity identity, gross ≥ net | `test_accounting_identity_and_costs` |
| Transaction costs (STT, stamp, GST base, SEBI fee, date-effective change, stress) | `test_costs.py` (9) |
| Corporate actions | split, bonus, rights TERP, dividend, symbol-change chain |
| Missing / duplicate data | data-quality tests, quarantine, idempotent re-import, missing-bar write-off |
| Backtesting realism | t+1 execution, reproducibility, participation cap, circuit lock, gap stop, dividends, no pyramiding |
| Drawdowns / metrics / statistics | `test_metrics_stats.py` (PSR, DSR, PBO noise vs skill, bootstrap) |
| Paper-order execution, persistence, restart, idempotency | `test_paper_scheduler.py` (crash on the 5th commit, then resume with no duplicates) |
| Scheduler restart / locks / duplicate runs | lock contention, stale-lock takeover, resume after failed step, skip completed |
| Audit logs | signal reasons and indicator JSON, fill → order linkage, test-mode tagging, audit rows per session |
| Research pipeline | split ordering, walk-forward never touches the test period, robustness keys, report sections, test-evaluation log |

## 3. Data sources connected
| Source | Status |
|---|---|
| Licensed NSE/BSE EOD data | **None connected.** Offline CSV/Parquet import is ready. |
| Licensed vendor API | Template only (`AuthorisedVendorProvider`) |
| Synthetic generator | Used for software testing only. Flagged in provenance, file names, reports and the dashboard banner. |

## 4. Strategies implemented
`trend_ma` (Trend), `xs_momentum` (Momentum), `rsi_mean_reversion` and `bollinger_reversion` (Mean Reversion), `donchian_breakout` (Breakout), `vol_breakout` (Volatility). Regime detection and portfolio construction (equal, inverse-volatility, risk parity) are also included.

## 5–6. Validation outcome (SYNTHETIC data, latest demo run)
| Strategy | Status | Score | WF OOS Sharpe | Final-test Sharpe | PBO | Sharpe at 2× costs | Failed critical gates |
|---|---|---|---|---|---|---|---|
| xs_momentum | Rejected | 42.7 | −0.14 | **1.42** | 0.23 | −0.10 | WF OOS ≤ 0; fails 2× costs |
| vol_breakout | Rejected | 37.1 | −0.24 | **1.15** | 0.94 | −0.28 | WF OOS ≤ 0; PBO; costs |
| trend_ma | Rejected | 34.3 | −0.28 | **1.36** | 0.81 | −0.17 | WF OOS ≤ 0; PBO; costs |
| donchian_breakout | Rejected | 30.0 | −0.43 | **1.12** | 0.48 | −0.18 | WF OOS ≤ 0; costs |
| bollinger_reversion | Rejected | 29.1 | −1.18 | 0.37 | 0.21 | −1.48 | WF OOS ≤ 0; costs |
| rsi_mean_reversion | Rejected | 21.8 | −1.49 | −0.47 | 0.02 | −1.98 | WF OOS ≤ 0; test ≤ 0; costs |

Sharpe figures are in excess of the 6.5% risk-free rate. DSR is about 0 for all six.

**Passed historical validation: none. Failed: all six.**

**Key lesson from the audit.** Four strategies posted final-test Sharpe ratios above 1.1 on data that, by construction, contains no edge: a long-only portfolio simply rode a rising synthetic market in the test window. Any single-period backtest would have looked attractive. The multi-test framework (walk-forward, cost stress, PBO, DSR) correctly rejected every one. The test-mode paper portfolio also gained about 45% over its year for the same reason. This is why the platform caps synthetic-data statuses and does not rank on returns.

The multi-strategy combination of walk-forward OOS streams was also negative (Sharpe −0.5 to −0.9). The strategies were highly correlated (e.g. trend vs momentum 0.87), reflecting their shared long-market beta.

## 7. Backtesting limitations
Daily bars only (intraday order of stop versus close is unknown). Execution is modelled at the open. Circuit detection is heuristic without a band file. Results are pre-tax. Gross equity is an approximation. Merger consideration is not credited without a factor. Static sectors. Full list in [LIMITATIONS.md](LIMITATIONS.md).

## 8. Risk-management capabilities
ATR risk sizing; caps on position weight, sector, gross exposure (no leverage), liquidity (ADV) and correlation; volatility targeting; regime exposure caps and entry blocks; daily and weekly loss limits (soft); drawdown warning; **drawdown kill switch (hard, manual reset with operator and note, persisted, audited)**; strategy suspension after consecutive losses; gap alerts; protective stops with gap and circuit handling; time stops; write-off of stale or delisted positions. Pyramiding, averaging down and martingale are impossible by construction. In research mode, hard limits are logged as "would have triggered" (documented in RISK_METHODOLOGY.md).

## 9. Test results
`80 passed` (unit and integration, about 35 seconds). `ruff check`: all checks passed.

## 10. Remaining technical limitations / open items
1. Connect licensed data. Until then, no evidence about Indian markets exists.
2. Import official NSE/BSE holiday lists, and verify the `verify`/`assumption` rows in `transaction_costs.yaml`, especially BSE rates and pre-2013 and pre-2020 statutory rates.
3. The daily workflow re-validates the full history every run (about 15–20 s per run on 240k rows). This is acceptable for one run a day; it could be made incremental for much larger universes.
4. The dashboard has no authentication. The file lock assumes a single host.
5. DSR counts only trials run inside the platform.

## 11. Operating instructions (summary)
Run each command on its own:

```bash
pip install -e ".[dev]"
```

```bash
python main.py init
```

```bash
python -m pytest
```

```bash
python main.py import --dataset prices --file <licensed file> --license-note "<licence>"
```

```bash
python main.py validate-data
```

```bash
python main.py research
```

```bash
python main.py paper run
```

```bash
streamlit run dashboard.py
```

```bash
python main.py schedule daily
```

Scheduling (cron, IST) and recovery are covered in [TROUBLESHOOTING.md](TROUBLESHOOTING.md). Kill-switch handling is covered in [PAPER_TRADING_MANUAL.md](PAPER_TRADING_MANUAL.md).
