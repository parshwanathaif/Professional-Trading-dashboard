# Phase 1 Research Notes: The Indian Cash-Equity Market for Systematic Research

This note covers the facts that shape how the platform is designed. Sources and verification status are in [REGULATORY_REFERENCE.md](REGULATORY_REFERENCE.md). Items marked *(knowledge)* come from domain knowledge and were not re-verified online during the build.

## 1. Market structure
- **Venues.** There are two national exchanges for cash equities: **NSE**, which has most of the cash-equity turnover, and **BSE**, which lists many more companies, often illiquid ones. Clearing is done by NSE Clearing and ICCL respectively, and settlement goes through the depositories (NSDL and CDSL).
- **Session (IST)** *(knowledge)*. Pre-open call auction runs 09:00–09:08. The continuous session runs 09:15–15:30. The closing price is a volume-weighted average of the last 30 minutes, not the last trade. The post-close session follows. Design consequence: a daily close signal can only be acted on at the next session. The platform enforces execution at t+1 or later.
- **Settlement** *(knowledge)*. Rolling T+1 for equities. Delivery-based positions are modelled, so STT and stamp duty apply at delivery rates.
- **Series.** EQ (normal rolling settlement) is the default research universe. BE/BZ (trade-for-trade) and SM (SME) series have different liquidity and rules and should be excluded or handled separately. The `series` column is stored for this reason.

## 2. Trading mechanisms that affect simulation realism
- **Price bands (circuit filters).** Stocks have a static daily band of 2/5/10/20%. Stocks with derivatives instead have dynamic operating ranges. A stock locked at its upper band has no sellers, and one locked at its lower band has no buyers. The simulator therefore blocks buys into upper locks and sells into lower locks, and retries the sells.
- **Market-wide circuit breakers.** Index moves of 10/15/20% halt all equity and equity-derivative trading. Missing bars on halt days are never filled.
- **Trade-to-trade / surveillance (ASM/GSM).** These stocks often show poor liquidity and extreme moves. A licensed surveillance list can be imported as `price_bands` or used to exclude symbols.
- **Liquidity.** Spreads and market impact vary enormously between NIFTY 50 names and small caps. The cost model uses a spread schedule bucketed by ADV and square-root impact. Sizing is capped at 2% of 20-day median traded value, and fills at 5% of session volume.

## 3. Corporate actions
Corporate actions include splits (face-value sub-division), bonus issues, rights issues (adjusted with the TERP factor), dividends (frequent, interim and final), symbol changes, mergers, demergers (which need a provider-supplied factor) and delistings (voluntary or compulsory). Unadjusted exchange prices are stored. Adjusted prices are derived reproducibly. Dividends are credited as cash on the ex-date.

## 4. Indices and benchmarks
- **NIFTY 50** is free-float market-cap weighted, rebalanced semi-annually. NIFTY 500 is the broad-market proxy. The BSE SENSEX has 30 constituents.
- **Survivorship bias.** Today's NIFTY 50 constituents are the survivors. Backtesting them over 15 years overstates returns. The platform needs **point-in-time membership** (`index_membership` with start and end dates) and **delisted securities** before it labels results survivorship-safe. Otherwise it caps the research status and says why.
- Total-return index (TRI) data should be preferred for benchmark comparison when available. A price index understates the benchmark by the dividend yield, about 1–1.5% a year for NIFTY *(knowledge)*.

## 5. Costs (delivery, per side unless noted)
| Component | Rate used (from 2026-03-01) | Notes |
|---|---|---|
| STT | 0.1% buy and 0.1% sell | Unchanged by the Finance Act 2026 |
| Exchange transaction charge (NSE) | ~0.003069% | With IPFT, about ₹307/crore in total |
| SEBI turnover fee | 0.0001% | ₹10/crore |
| Stamp duty | 0.015% | Buyer only |
| GST | 18% | On brokerage, exchange charges and SEBI fee |
| DP charge | ~₹15.93 | Per scrip per sell day; broker-specific |
| Brokerage | 0.03% (assumed) | Configurable; set your broker's actual rate |

The statutory round trip is about **0.25%** before spread and impact. That matters a great deal for high-turnover strategies such as short-term mean reversion.

## 6. Regimes and volatility
Indian equities have gone through distinct regimes: the 2008 crisis, 2011 and 2013 drawdowns (the taper tantrum and INR stress), the 2015–16 correction, the 2018 mid/small-cap bear market after the IL&FS crisis, the 2020 COVID crash with a V-shaped recovery, and the 2021–24 broad rally *(knowledge)*. Mid and small caps behave differently from large caps. The regime detector uses the benchmark's own expanding history (percentiles), so no thresholds are fitted on the future.

## 7. Biases and statistical validity
- **Look-ahead.** Signals use the close of t. Fills happen at the open of t+1 or later. A causality test is run on every strategy. Absolute-price filters use raw prices, not back-adjusted ones.
- **Survivorship.** See section 4.
- **Selection bias / multiple testing.** Every parameter combination of every strategy counts as a trial. Results are reported with the Deflated Sharpe Ratio and PBO.
- **Overfitting.** Parameters are chosen on the training period only. Walk-forward never touches the final test period. The test period is evaluated once and every evaluation is logged.
- **Non-normality.** Fat tails and skew are reflected in PSR/DSR and CVaR.

## 8. Risk management principles adopted
Capital preservation comes first. Risk per position is set relative to an ATR stop. There are caps on position weight, sector and gross exposure (no leverage), and liquidity. Volatility targeting is applied. Daily and weekly loss limits act as soft blocks. A drawdown kill switch is a hard block that needs a manual reset. Strategies are suspended after a run of consecutive losses. New entries are gated in stress regimes. Martingale, averaging down and pyramiding cannot happen by construction.
