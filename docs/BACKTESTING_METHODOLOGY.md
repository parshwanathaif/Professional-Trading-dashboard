# Backtesting & Validation Methodology

## Simulation (event-driven, daily)
For each session *t* the `PortfolioSimulator` runs these steps in order:
1. **Execution at the open of *t*.** It fills orders created at the close of *t − delay* (delay ≥ 1, enforced by configuration validation). Sells are filled before buys. A fill is refused when:
   - the bar is missing (halt, suspension or no data); sells are retried;
   - the stock is locked at its upper circuit (buys) or lower circuit (sells). A lock means high = low and the move reaches the band, or about 2% when the band is unknown;
   - the quantity exceeds `max_participation_of_volume` × the session's volume. Buys are partially filled; the unfilled part of a sell is retried;
   - cash is insufficient. There is never any borrowing.
2. **Fill price.** `open × (1 ± (half_spread(ADV bucket) + base_slippage + impact))`, where `impact = coef · σ_daily · √(qty/volume)`. Statutory charges come from the date-effective schedule.
3. **Stops.** Checked against the low. A gap through the stop fills at the open.
4. **Dividends.** Credited on the ex-date for positions held at the previous close.
5. **Write-offs.** A position with no price for more than 5 sessions is closed at last price × (1 − 30% haircut). This is causal: it does not use knowledge of a future delisting.
6. **Mark-to-market** at the close. Equity and gross equity are recorded, where gross = net + cumulative charges + execution costs.
7. **Risk update.** Loss limits and drawdown checks.
8. **Decisions at the close.** Exits (signal, strategy time stop, risk time stop), then entries ranked by score, each passed through the risk engine.

Signals are computed on the full panel once per parameter set. This is legitimate because every indicator is causal; the leakage test verifies it, and running the simulator to date *t* gives identical results to a run truncated at *t*.

## Prices
Back-adjusted prices (splits, bonus issues, rights via TERP, explicit demerger factors) are used for signals and P&L, so quantities are in adjusted units. Absolute-price rules such as the minimum-price filter use **raw** prices, so later corporate actions cannot leak into earlier universe decisions.

## Validation protocol (per strategy)
| Step | Data used | Purpose |
|---|---|---|
| Parameter grid | **Training** (first 60% after a 300-session warm-up) | Select parameters by net Sharpe (excess of risk-free), with a minimum trade count |
| Validation | Next 20% | Report only; never used for selection |
| Walk-forward | Pre-test history: rolling 4y train / 1y test, re-optimised each fold | Stitched out-of-sample record and fold consistency |
| **Final test** | Last 20%, evaluated **once** with locked parameters; every evaluation is logged to `reports/research/test_evaluations.jsonl` | Untouched out-of-sample estimate |
| Robustness | Pre-test period | Costs ×2 and ×3, slippage ×2 and ×4, delay 2–3 sessions, 1% participation, +150 bps adverse fills, 2% random missing bars, parameter neighbourhood |
| Statistics | Walk-forward OOS and test | PSR, DSR (all trials from all strategies), stationary-bootstrap Sharpe CI, Monte Carlo 1-year paths, trade-order Monte Carlo, PBO via CSCV over the grid, skew, kurtosis and Jarque–Bera, fold Sharpe dispersion |
| Regimes | Walk-forward OOS, test, training | Where the strategy made or lost money |

### Research status gates (config: `validation.gates`)
- **Rejected** if any critical gate fails: the leakage test, walk-forward OOS Sharpe > 0, test Sharpe > 0, PBO ≤ 0.5, or Sharpe at 2× costs > 0.
- **Insufficient evidence** if the out-of-sample trade count is below 30.
- **Needs further validation** if any standard gate fails: WF OOS Sharpe ≥ 0.3, DSR ≥ 0.90, OOS drawdown ≥ −35%, ≥ 50% positive folds, or ≥ 60% of the grid positive.
- **Passed historical validation** if all of the above pass.
- **Eligible for paper-trading evaluation** additionally needs DSR ≥ 0.95 and a positive Sharpe in every robustness scenario.
- **Data caps.** Synthetic data caps the status at *Insufficient evidence*. A universe that is not point-in-time caps it at *Needs further validation*.

The **score (0–100)** weights out-of-sample performance (15%), final test (10%), drawdown (10%), Sortino (5%), fold consistency (10%), sample size (5%), cost sensitivity (10%), parameter stability (10%), regime robustness (10%), liquidity (5%) and DSR (10%). The score ranks strategies. It never overrides the gates.

## Metrics
Net figures (after all simulated costs) are primary, and gross figures are shown alongside. Reported: initial and final capital, absolute P&L, CAGR, annualised return, max drawdown, Sharpe, Sortino, Calmar, profit factor, win rate, average win and loss, risk/reward, average holding period, annual turnover, transaction costs (statutory and execution), exposure, volatility, VaR and CVaR, recovery period, and benchmark CAGR, Sharpe, beta and information ratio. Sharpe and Sortino are reported **in excess of the 6.5% risk-free rate**.
