# Installation Guide

Requirements: Python 3.11 or newer (developed on 3.13), about 1 GB of disk for a 15-year NSE dataset, and no network access once installed.

## 1. Create an isolated environment

```bash
cd bharat_quant_lab
```

```bash
python3 -m venv .venv
```

```bash
source .venv/bin/activate
```

On Windows: `.venv\Scripts\activate`.

## 2. Install pinned dependencies

```bash
pip install -e ".[dev]"
```

The versions are pinned in `pyproject.toml`: numpy 2.5.3, pandas 3.0.5, pyarrow 26.0.1, duckdb 2.0.1, pydantic 2.13.5, PyYAML 6.0.3, scipy 1.18.1, plotly 7.1.0, matplotlib 3.11.2, streamlit 1.67.0, openpyxl 3.1.5, reportlab 5.0.1, markdown-it-py 4.3.0, pytest 9.1.1, ruff 0.17.6.

## 3. Initialise the workspace

```bash
python main.py init
```

This creates `data/processed/market.duckdb`, `state/paper_trading.sqlite`, the `data/raw/inbox` and `data/raw/archive` folders, `reports/` and `logs/`.

## 4. Verify the installation

```bash
python -m pytest
```

```bash
ruff check src tests main.py dashboard
```

## 5. Optional: run the synthetic end-to-end demo

```bash
python main.py demo --fresh
```

```bash
BQL_ROOT=$PWD/demo_workspace streamlit run dashboard.py
```

The demo builds its own isolated `demo_workspace/`, so it never touches your real database.

## 6. Configure for real research
1. Copy `.env.example` to `.env`. Credentials are only needed for a licensed vendor plug-in.
2. Edit `config/settings.yaml`: set `backtest.benchmark` (for example `NIFTY50`), `data.universe_index` (for example `NIFTY500`) and your capital.
3. Edit `config/transaction_costs.yaml` with your broker's brokerage and DP charges, and verify every `verify` row.
4. Import data. See [DATA_IMPORT_GUIDE.md](DATA_IMPORT_GUIDE.md).
