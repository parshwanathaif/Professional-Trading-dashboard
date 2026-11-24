# Data Import Guide

## One-off import of a file

```bash
python main.py import --dataset securities --file path/to/securities.csv --license-note "Vendor X EOD licence #123"
```

```bash
python main.py import --dataset corporate_actions --file path/to/corporate_actions.csv
```

```bash
python main.py import --dataset index_membership --file path/to/nifty500_membership_pit.csv
```

```bash
python main.py import --dataset holidays --file path/to/nse_holidays_2010_2026.csv
```

```bash
python main.py import --dataset benchmarks --file path/to/nifty50_tri.csv
```

```bash
python main.py import --dataset prices --file path/to/nse_eod_2010_2026.parquet
```

Import order matters for readability, not correctness. Adjustments are computed at read time, so corporate actions can arrive after prices.

## Daily incremental import (inbox)
Drop files into `data/raw/inbox/`, named with the dataset prefix (`prices_20261123.csv`, `benchmarks_20261123.csv`, `corporate_actions_20261123.csv`). Then either run the daily workflow, which imports them automatically, or run:

```bash
python main.py import --inbox
```

## Column reference (minimum)
| Dataset | Required | Optional |
|---|---|---|
| prices | date, symbol, open, high, low, close, volume | exchange (NSE), value, series (EQ) |
| securities | symbol | exchange, isin, name, sector, industry, listing_date, delisting_date, delisting_reason |
| corporate_actions | symbol, ex_date, action_type | exchange, ratio_from, ratio_to, cash_amount, rights_price, new_symbol, adjustment_factor, notes |
| index_membership | index_name, symbol, start_date | end_date (blank = still a member) |
| benchmarks | date, index_name, close | open, high, low |
| holidays | date | exchange, description (`special session` marks e.g. Muhurat), source, verified |
| price_bands | symbol, start_date, band_pct (0.05 = 5%) | end_date, exchange |

### Corporate-action ratio conventions
- split `1:5`: `ratio_from=1, ratio_to=5`, meaning 1 old share becomes 5 new.
- bonus `1:1`: `ratio_from=1` (held), `ratio_to=1` (bonus).
- rights `1 for 4 at ₹50`: `ratio_from=4, ratio_to=1, rights_price=50`.
- dividend: `cash_amount` per share, in INR, unadjusted.
- symbol change: `new_symbol`.
- demerger: `adjustment_factor` from the exchange or vendor (≥ 1).

## Check quality after every import

```bash
python main.py validate-data
```

The HTML, JSON and CSV report is written to `reports/data_quality/`. The process exits with code 2 if the dataset is blocked.

## Provenance
`SELECT * FROM provenance` in `data/processed/market.duckdb` shows the source, file SHA-256, licence note, row counts (inserted, duplicate, quarantined), IST timestamp and synthetic flag for every import. Conflicting rows are kept in `quarantine` for review. They are never applied silently.

## Licensed vendors
Implement `AuthorisedVendorProvider.fetch` in `src/bql/data/providers.py` against your vendor's documented API, and keep credentials in `.env` (`BQL_VENDOR_API_KEY`). Do not scrape websites or bypass access controls.
