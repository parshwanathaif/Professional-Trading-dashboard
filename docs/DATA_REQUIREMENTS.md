# Historical Data Requirements

## Minimum for institutional-grade evidence
| Dataset | Required fields | Why |
|---|---|---|
| Daily OHLCV, **unadjusted** | date, symbol, exchange, open, high, low, close, volume, value (traded value, INR), series | Raw exchange record. Adjustments are derived reproducibly. |
| Securities master **including delisted** | symbol, isin, name, sector, listing_date, delisting_date, delisting_reason | Prevents survivorship bias. Gives sector limits. Drives delisting handling. |
| Corporate actions | symbol, ex_date, action_type (split, bonus, rights, dividend, symbol_change, merger, demerger), ratios, cash_amount, rights_price, new_symbol, adjustment_factor | Price continuity, dividends, symbol continuity. |
| Point-in-time index membership | index_name, symbol, start_date, end_date | Universe as it was known on each date. |
| Benchmark index levels (TRI preferred) | date, index_name, close (open/high/low optional) | Regimes, benchmark comparison, calendar reference. |
| Exchange holiday list | date, exchange, description, source, verified | Calendar and missing-bar detection. |
| Price bands (optional but recommended) | symbol, start_date, end_date, band_pct | Circuit-lock detection. |

**Coverage:** 15 or more years, so the data spans the 2008, 2011, 2013, 2015–16, 2018, 2020 and 2022 stress periods. EQ series only, unless you deliberately study other series.

## File conventions (`data/raw/inbox`)
File names must start with the dataset name, e.g. `prices_nse_2026-11-20.csv`, `corporate_actions_2026q4.parquet`, `benchmarks_nifty50.csv`. The inbox importer processes them in dependency order and moves each file to `data/raw/archive/` with a timestamp.

Dates are ISO `YYYY-MM-DD` and interpreted in Asia/Kolkata. Timezone-aware values are converted to IST. Numbers are plain decimals. Column names are case-insensitive. Extra columns are ignored.

## What the platform does with imperfect data
| Situation | Behaviour |
|---|---|
| Required column missing / unparseable date | Import rejected (`DataImportError`) |
| Exact duplicate row (same file or re-import) | Skipped and counted in provenance |
| Same key, different values | Written to `quarantine`; existing value kept; flagged in DQ report |
| Invalid OHLC, non-positive price, negative volume, future date | CRITICAL; symbol excluded from the run |
| Missing bars > 5% of a symbol's listed life, or a gap > 20 sessions | CRITICAL; symbol excluded |
| A few missing bars | WARNING; never filled. Indicators tolerate up to 10% missing in a window. No trade can fill on a missing bar. |
| Benchmark missing | Dataset CRITICAL; run blocked |
| Unexplained move > 25% | WARNING for review (an explained corporate action is INFO) |
| Bar on weekend/holiday | WARNING (possible Muhurat/special session) |
| Latest session incomplete (< 90% of previous session's symbols) | Paper trading stops for that session |
| No `value` column | Approximated as close × volume and recorded in provenance notes |

## Survivorship-safety rule
A panel is labelled `survivorship_safe` only when point-in-time membership is used AND the dataset contains delisted securities or ended memberships. Otherwise research statuses are capped at "Needs further validation" and every report carries the warning.

## Legal
Import only data you are licensed to use. The platform contains no scraper. `AuthorisedVendorProvider` is a template for a licensed API: credentials go in environment variables, and its terms and rate limits must be respected.
