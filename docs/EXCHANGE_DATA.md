# Official NSE / BSE Exchange Data

The platform downloads end-of-day files directly from the exchanges' public download archives. No third-party vendor is involved.

```bash
python main.py fetch-exchange
```

The first run downloads the history from `exchange_data.start_date`. Later runs are incremental, and the daily workflow calls this automatically after the close.

## Sources
| File (published by) | URL pattern | Used for |
|---|---|---|
| NSE CM bhavcopy, legacy format (until 05-Jul-2024) | `nsearchives.nseindia.com/content/historical/EQUITIES/YYYY/MON/cmDDMONYYYYbhav.csv.zip` | Prices |
| NSE CM bhavcopy, UDiFF format (from 08-Jul-2024) | `nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip` | Prices |
| NSE daily index closes | `nsearchives.nseindia.com/content/indices/ind_close_all_DDMMYYYY.csv` (available from 2013) | Benchmarks (NIFTY 50, NIFTY 500, sector indices, India VIX) |
| NSE securities list | `/content/equities/EQUITY_L.csv` | Names, ISINs, listing dates |
| NSE delisted companies | `/content/equities/delisted.csv` | Delisting dates and types |
| NSE symbol changes | `/content/equities/symbolchange.csv` | Symbol continuity |
| NSE daily market-report archive "PR" (weekly sample) | `nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PRDDMMYY.zip` → `Bc*.csv` | Corporate actions: bonus, split, consolidation, rights, demerger, **dividends**, with ex/record dates |
| NSE price bands (current) | `/content/equities/sec_list.csv` | Circuit bands, from the fetch date onward |
| NSE index constituent lists | `/content/indices/ind_nifty500list.csv`, `ind_niftytotalmarket_list.csv`, `ind_niftymicrocap250_list.csv` | Industry/sector classification |
| BSE equity bhavcopy (UDiFF) | `bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_YYYYMMDD_F_0000.CSV` | Independent NSE vs BSE price cross-check |

## How the data are accessed
- Only public download files are requested. There is no login and no API session. The `www.nseindia.com` front end uses bot protection, and it is **not** circumvented; the archive host provides everything needed.
- Requests use an honest User-Agent, run sequentially, and are spaced at least `min_request_interval_sec` apart (default 0.35 s; the configuration refuses anything below 0.25 s).
- Every file is cached under `data/raw/exchange/` with its SHA-256 in `manifest.json`, so nothing is downloaded twice and downloads resume after interruption. A 404 is recorded. Recent 404s are re-checked, because a file may not have been published yet.
- An HTML page returned instead of a data file is treated as "not available" and never parsed.
- Provenance records the source as `NSE_BHAVCOPY_ARCHIVE:*` with the licence note *"Official NSE/BSE public website archives; personal research use subject to the exchanges' website terms; not for redistribution"*. **Do not redistribute the raw files or the database.**

## What is derived, and how
| Item | Method | Caveats |
|---|---|---|
| **Corporate actions** | The purpose text in NSE's book-closure files is parsed: "BONUS 1:2", "FV SPLT FRM RS 10 TO RS 2", "CONSOLIDATION…", "RIGHTS 7:5 @ PRM RS 8", "DEMERGER", "DIV-RS 3.5 / SPL DIV-RS 2". The ex-date is the published EX_DT; otherwise it is the record date (T+1 era, from 27-Jan-2023) or the record date minus one session (T+2 era). **Splits, bonuses and consolidations with factors of 28% or more are accepted only if the actual price gap within ±3 sessions matches the ratio, and they are snapped to that session.** Smaller ones use the published ex-date. | Rights price = premium + *current* face value when only the premium is published (approximation). Demerger factor is price-implied (close before ÷ open on the ex-date). Dividend text without an amount (e.g. "DIVIDEND") or in % of face value is not captured; the count is reported in `build_summary.json`. |
| **Missed sessions** | The bhavcopy "previous close" is **unadjusted** (verified: HDFC Bank's 2019 split shows the raw previous close). When at least 30% of stocks report a previous close that differs from the previous downloaded session, a session was missed (weekend Muhurat or special Saturday session). Those dates are fetched automatically and marked as special sessions in the calendar. | |
| **Symbol changes** | NSE's `symbolchange.csv`, accepted only when the traded data confirm a clean hand-over: the old symbol stops before the new one starts, within ±10 days of the effective date | Rejected rows are simply not applied |
| **Delistings** | NSE's `delisted.csv`, otherwise "no NSE trades for more than 45 days before the latest session" | Merger share-swaps are not credited. The simulator writes off positions after 5 sessions without a price, with a 30% haircut. |
| **Holidays** | Weekdays on which NSE published neither the bhavcopy nor the index file, plus 25-Dec-2026 from the 2026 circular (marked `verify`) | |
| **Index name continuity** | "S&P CNX Nifty" → "CNX Nifty" → "Nifty 50" all map to `NIFTY50`. Similarly NIFTY500, NIFTYNEXT50, NIFTYBANK, NIFTYMIDCAP100 and others. | |
| **Sector** | Current NSE industry from the index constituent lists. Securities not in those lists are labelled `UNCLASSIFIED`. | Static, current classification. The 25% sector cap applies to `UNCLASSIFIED` as one bucket, which is conservative. |

## Universe: survivorship-safe without index-membership history
Point-in-time NIFTY membership history is not published in the archives. The research universe is therefore `liquidity_top_n`: on each date, the top N stocks (default 300) by trailing 126-day median traded value, among stocks passing the ₹5 crore/day liquidity floor, a ₹20 raw-price floor and a 252-session listing age. The universe uses **only data available on that date**. Because the bhavcopy archive contains **every security that traded on every day**, including those later delisted, merged or suspended, the universe is survivorship-safe. The research engine labels it as such (`complete_exchange_history`).

## Data-quality policy for exchange data
- **Missing sessions.** A stock absent from a bhavcopy did not trade, because of suspension or illiquidity; this is a fact, not missing data. Gaps are reported (`gaps_are_non_trading: true`) but do not exclude the stock, since excluding suspended stocks would itself introduce survivorship bias. No trade can ever fill on a session without a bar.
- **Invalid rows.** A single invalid bar is excluded on its own and treated as no trade (`critical_row_policy: exclude_rows`), rather than dropping the stock.
- **Series.** EQ, BE and BZ are kept; SME and debt series are excluded. Where a symbol appears in two series on the same day, EQ wins.
- **Cross-check.** NSE and BSE closes are compared by ISIN and date for 2024 onward. Results are in `data/processed/exchange_build/nse_bse_crosscheck*.{json,csv}` and on the dashboard Market overview.

## Network access
The cloud environment's allow-list must include `nsearchives.nseindia.com`, `www.bseindia.com` and (optionally) `archives.nseindia.com`, `www.nseindia.com`, `www.niftyindices.com` and `api.bseindia.com`.
