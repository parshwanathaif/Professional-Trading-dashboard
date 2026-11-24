# Limitations & Known Assumptions

## Data
1. Market data come from **official NSE/BSE public archives** (see EXCHANGE_DATA.md) for personal research. NSE history starts in 2013, because daily index files are not archived earlier. BSE is used from 2024 for cross-checking only.
1a. Dividends come from NSE book-closure purpose text. Amounts given only as "DIVIDEND" or as a % of face value are missed, so dividend income is slightly understated. The NIFTY benchmark used is a price index, so it excludes dividends; strategy results include the dividends captured.
1b. **Point-in-time index membership is not available** from the archives. The universe is a causal top-N-by-liquidity rule instead. It is survivorship-safe because the bhavcopies include every traded security, but it is not identical to NIFTY 500 membership.
1c. Sector classification is the **current** NSE industry. Stocks that have since delisted or merged are `UNCLASSIFIED`.
1d. Rights prices (when only the premium is published) and demerger factors are approximations.
2. For user-supplied (non-exchange) files, survivorship-safety depends on also supplying point-in-time membership and delisted securities.
3. Sector classification is static per security (taken from the securities master). Historical reclassifications are not modelled.
4. Mergers end a series. Share-swap consideration is not credited unless the provider expresses it as an adjustment factor. Demergers need a provider-supplied factor.
5. The benchmark should be a total-return index. A price index understates the benchmark by its dividend yield.

## Execution model
1. Daily bars only. Intraday paths are unknown: a stop and a target within the same bar cannot be ordered, and stops fill at the stop price or the open.
2. Fills at the open with modelled spread, slippage and square-root impact. Real opening auctions and spreads may differ, especially for small caps. Stress tests (costs ×2/×3, slippage ×2/×4, +150 bps) bound this.
3. Circuit detection without a price-band file is a heuristic (high = low and |move| ≥ about 2%).
4. Quantities are in adjusted-share units, and lot rounding to raw shares is ignored. This is negligible for cash equities, where the lot size is 1.
5. Short selling, intraday, F&O, leverage, SLB, pledging and margin are deliberately not modelled.
6. Taxes on gains (STCG/LTCG) are not modelled. Results are pre-tax.
7. Gross equity is approximated as net equity plus cumulative costs, so it ignores the compounding of costs.

## Statistical
1. DSR uses the trials in the current research run. Earlier exploratory research done outside the platform is not counted, so treat DSR as an upper bound.
2. PBO uses the grid of each strategy. Small grids give coarse estimates.
3. Walk-forward slices continuous runs. Fold boundaries carry open positions across, which is realistic for a live process but not a fresh-capital restart.
4. The regime classifier is rule-based, and its thresholds (20%/80% percentiles, 15% drawdown) are judgement calls.

## Operational
1. The scheduler's file lock assumes a single host.
2. The dashboard has no authentication. Run it locally or behind your own access control.
3. Holiday calendars are not shipped. You must import the official lists.
4. BSE charge rows and pre-2013/pre-2020 statutory rates are approximations flagged `verify` or `assumption`.
