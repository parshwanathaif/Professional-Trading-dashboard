# Bharat Quant Lab

An institutional-style **quantitative research and automated PAPER-TRADING platform for Indian cash equities** (NSE first, BSE where data allow), built in Python.

> **Research, historical simulation and paper trading only.** The platform has no broker connectivity, no live order placement, no leverage and no derivatives. Historical results are not a forecast or guarantee: strategies can and do lose money.

## What it does
- **Data engine.** Imports CSV or Parquet into DuckDB with provenance (file hash, licence note, IST timestamp) and quarantines conflicting duplicates. A data-quality gate runs before every run. Corporate actions are handled (split, bonus, rights via TERP, dividends, symbol changes, demerger factors, delistings), along with point-in-time index membership, price bands and an NSE calendar with special sessions. Missing prices are never filled.
- **Strategies.** Six causal strategies across trend, momentum, mean reversion (two), breakout and volatility, plus a rule-based market-regime classifier.
- **Backtester.** Event-driven. Signals at close t execute at the open of t+1 or later. Includes date-effective Indian statutory charges (STT, exchange, SEBI, stamp duty, GST, DP), spread, slippage and square-root impact, volume participation caps, circuit locks, gap-aware stops, dividends, write-offs and cash accounting.
- **Validation.** Train/validation/untouched test split, walk-forward, a leakage test, and robustness to costs, slippage, delay, liquidity, gaps, missing data and parameters. Statistics: PSR, Deflated Sharpe, bootstrap, Monte Carlo and PBO (CSCV). Regime analysis, multi-dimensional scoring and gated research statuses.
- **Risk engine.** Independent of strategies. ATR risk sizing, weight, sector, exposure, liquidity and correlation limits, volatility targeting, daily and weekly loss limits, a drawdown **kill switch** with manual reset, strategy suspension and regime gating.
- **Paper trading.** The same simulator, persisted in SQLite. Sessions are atomic and idempotent, with a full audit trail.
- **Automation.** Daily, weekly and monthly workflows with locks, a run registry, restart from the failed step, and duplicate-run protection.
- **Dashboard and reports.** A 7-section Streamlit dashboard. Reports in Markdown, HTML (offline charts), CSV, Excel and PDF.

## Quick start

```bash
pip install -e ".[dev]"
```

```bash
python main.py init
```

```bash
python -m pytest
```

Run the whole pipeline on synthetic data in an isolated `demo_workspace/`. This takes about 10 minutes.

```bash
python main.py demo --fresh
```

```bash
BQL_ROOT=$PWD/demo_workspace streamlit run dashboard.py
```

With official NSE/BSE data (downloaded from the exchanges' public archives, resumable and rate-limited, personal research use; see [docs/EXCHANGE_DATA.md](docs/EXCHANGE_DATA.md)):

```bash
python main.py fetch-exchange
```

Or with your own licensed files:

```bash
python main.py import --dataset prices --file data/raw/your_prices.csv --license-note "your licence"
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
python main.py schedule daily
```

## Documentation
| Topic | File |
|---|---|
| Installation | [docs/INSTALLATION.md](docs/INSTALLATION.md) |
| Project plan & assumptions | [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md) |
| Market research (Phase 1) | [docs/MARKET_RESEARCH.md](docs/MARKET_RESEARCH.md) |
| System architecture | [docs/SYSTEM_ARCHITECTURE.md](docs/SYSTEM_ARCHITECTURE.md) |
| **Automated paper trading on GitHub Actions** | [docs/AUTOMATION.md](docs/AUTOMATION.md) |
| Official NSE/BSE exchange data | [docs/EXCHANGE_DATA.md](docs/EXCHANGE_DATA.md) |
| Data requirements / import guide | [docs/DATA_REQUIREMENTS.md](docs/DATA_REQUIREMENTS.md), [docs/DATA_IMPORT_GUIDE.md](docs/DATA_IMPORT_GUIDE.md) |
| Strategies (plain English + maths) | [docs/STRATEGIES.md](docs/STRATEGIES.md) |
| Backtesting & validation methodology | [docs/BACKTESTING_METHODOLOGY.md](docs/BACKTESTING_METHODOLOGY.md) |
| Risk methodology | [docs/RISK_METHODOLOGY.md](docs/RISK_METHODOLOGY.md) |
| Paper-trading operating manual | [docs/PAPER_TRADING_MANUAL.md](docs/PAPER_TRADING_MANUAL.md) |
| Dashboard guide | [docs/DASHBOARD_GUIDE.md](docs/DASHBOARD_GUIDE.md) |
| Troubleshooting, scheduling, backup | [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) |
| Regulatory reference register | [docs/REGULATORY_REFERENCE.md](docs/REGULATORY_REFERENCE.md) |
| Limitations | [docs/LIMITATIONS.md](docs/LIMITATIONS.md) |
| **Final audit report** | [docs/FINAL_AUDIT_REPORT.md](docs/FINAL_AUDIT_REPORT.md) |

## Current status
No licensed market data is connected. Every result produced so far comes from **synthetic test data with no embedded edge**. On that data the research engine **rejected all six strategies**, which is the correct outcome. See the audit report.
