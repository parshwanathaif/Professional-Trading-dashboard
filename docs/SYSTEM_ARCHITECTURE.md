# System Architecture

## Design principles
1. **One simulator, two uses.** The backtester and the paper-trading engine run the same `PortfolioSimulator`, so paper results are directly comparable with research results.
2. **Strategies are pure, causal signal functions.** They never see the portfolio, never size positions and cannot call the risk engine.
3. **An independent risk engine.** It is the only component that can size or permit an entry.
4. **Data immutability and provenance.** Raw prices are stored unchanged, adjustments are derived, and every row traces back to an import record.
5. **Fail closed.** Bad or incomplete data, a breached hard limit or a failed step all stop the run. Missing data never produces a default trade.
6. **Reproducibility.** Configuration hashes, deterministic seeds, run hashes and a logged final-test evaluation.

## Layout
```
bharat_quant_lab/
├─ config/                 settings, strategies, risk_limits, transaction_costs (YAML, Pydantic-validated, hashed)
├─ src/bql/
│  ├─ config.py            load + validate + hash configuration
│  ├─ logging_setup.py     JSON-lines structured logs (IST)
│  ├─ data/                schema, providers (CSV/Parquet + vendor template), DuckDB store, validation,
│  │                       corporate actions, panel builder, synthetic test generator
│  ├─ market_calendar/     NSE/BSE trading calendar (holidays, weekends, special sessions)
│  ├─ indicators/          causal indicators on (T,N) arrays
│  ├─ regime/              benchmark regime classifier (expanding percentiles)
│  ├─ strategies/          Strategy interface + 6 strategies across 5 families
│  ├─ costs/               date-effective statutory charges + spread/slippage/impact
│  ├─ risk/                independent risk engine (sizing, limits, kill switch, suspension)
│  ├─ backtesting/         PortfolioSimulator, run_backtest, metrics
│  ├─ validation/          splits, walk-forward, leakage test, statistics, research pipeline, scoring/status
│  ├─ portfolio/           multi-strategy combination (equal / inverse-vol / risk parity)
│  ├─ paper_trading/       SQLite ledger + PaperTrader (atomic, idempotent sessions)
│  ├─ reporting/           Plotly charts, strategy reports, period reports, Excel/PDF export
│  ├─ scheduler/           daily/weekly/monthly workflows, file locks, run registry, daemon
│  └─ pipeline.py          high-level operations shared by CLI, scheduler and dashboard
├─ dashboard/app.py        Streamlit dashboard (7 sections); dashboard.py is the entry point
├─ main.py                 CLI
├─ tests/                  pytest suite (synthetic data only)
├─ data/ reports/ logs/ state/   runtime artefacts (git-ignored)
└─ docs/
```

## Data flow
```
CSV/Parquet (licensed) ──► FileProvider.normalise ──► MarketStore.ingest ──► DuckDB (raw, provenance, quarantine)
                                                                          │
                     validate_prices (DQ report: block / exclude / warn) ◄┘
                                     │
               build_panel: symbol changes → corporate-action factors → adjusted (T,N) arrays,
                            point-in-time membership, price bands, sectors, delistings
                                     │
          ┌──────────────────────────┼─────────────────────────────┐
     Strategy.generate          detect_regimes                 universe_mask (causal)
          └────────────► PortfolioSimulator ◄── RiskManager ◄── CostModel
                         (execute → stops → dividends → write-offs → mark → risk → decide)
                 ┌───────────────┴──────────────────┐
           run_backtest → metrics            PaperTrader → PaperLedger (SQLite, atomic per session)
                 │                                   │
     research pipeline (train/val/test,       period reports, dashboard
     walk-forward, robustness, DSR/PBO, status)
```

## Storage
| Store | Technology | Contents | Concurrency |
|---|---|---|---|
| `data/processed/market.duckdb` | DuckDB | prices (raw), securities, corporate actions, index membership, benchmarks, holidays, price bands, provenance, quarantine | single writer (import); readers open read-only |
| `state/paper_trading.sqlite` | SQLite (WAL) | portfolios, processed sessions, state snapshots, signals, order events, fills, trades, equity, positions, risk events, audit log | one transaction per session; unique keys guarantee idempotency |
| `state/workflow_runs.sqlite` | SQLite | workflow run registry (status, completed steps, attempts) | file lock per workflow |
| `reports/` | files | data-quality, research (JSON, Parquet, MD/HTML/PDF), backtests, paper reports (MD/HTML/CSV/XLSX/PDF) | append-only |

## Why these choices
- **DuckDB + Parquet.** Columnar analytics with no database server, which suits offline research. It supports `INSERT … BY NAME` and fast pivots.
- **SQLite for the ledger.** ACID transactions and unique constraints are exactly what idempotent session commits need. It also tolerates the dashboard reading while a workflow writes (WAL).
- **NumPy (T,N) arrays in the simulator.** The day loop stays in Python for clarity and auditability. Indicators are vectorised. A 17-year, 60-stock backtest takes about 1–2 s.
- **Rule-based regimes rather than fitted models.** They are transparent and cannot be refit on the future.

## Security
- No credentials are needed in offline mode. Vendor credentials are read only from environment variables (`.env`, which is git-ignored; see `.env.example`).
- No network calls in the core library. No scraping.
- Inputs are validated with Pydantic on config load and schema-checked on data import.
- Backup: copy `data/processed/market.duckdb`, `state/` and `config/` (see TROUBLESHOOTING.md → Backup & recovery).
