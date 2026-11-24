# Risk Methodology

Capital preservation is the primary objective. The risk engine (`src/bql/risk/engine.py`) is independent of strategies and is the only component allowed to size positions or permit entries.

## Position sizing
For a candidate with close `P` and stop distance `D = k · ATR20` (k = 2.5 by default):

```
qty = floor( min( equity · r / D                         # risk budget (r = 0.75%)
                  equity · w_max / P                      # max position weight (8%)
                  ADV_participation · ADV20_value / P     # liquidity (2% of 20d median traded value)
                  (cap_exposure · equity − invested) / P  # gross exposure headroom
                  (s_max · equity − sector_value) / P     # sector headroom (25%)
                  (cash − buffer · equity) / (P·(1+c)) ))  # cash, never borrowed
```
An entry is rejected if `qty · P` is below the minimum ticket (₹25,000) or the maximum number of open positions (20) is reached. The binding constraint is recorded with every decision.

## Portfolio limits
- **Gross exposure.** Capped at 100% (no leverage). It is also reduced by **volatility targeting**: `cap = min(cap, target_vol / realised_vol_63d)` with a 15% target, and by **regime caps** (STRONG_BEARISH 50%, HIGH_VOLATILITY 70%, MARKET_STRESS 30%).
- **Correlation.** A candidate whose average 120-day return correlation with current holdings exceeds 0.75 is rejected.
- **One position per symbol.** Adds are never made to an existing position, so pyramiding and averaging down are impossible.

## Loss limits
| Limit | Default | Type | Effect |
|---|---|---|---|
| Daily loss | 3% | Soft | No new entries next session; clears automatically |
| Weekly loss | 6% | Soft | No new entries until the end of the ISO week |
| Drawdown warning | 10% | Alert | Logged and shown on the dashboard |
| **Drawdown kill switch** | 20% | **Hard** | No new entries until a **manual reset** with operator name and note. The event is recorded, and all records are preserved. |
| Consecutive losses | 8 trades | **Hard (per strategy)** | Strategy suspended until a manual reset |
| Gap risk | 8% adverse open | Alert | Logged per position |

Exits (signals, stops, time stops) keep working while entries are blocked, so the engine can always reduce risk.

## Stops
Protective stops are set at fill time: `stop = fill − D`. They are checked against each session's low. A gap through the stop fills at the open, which is worse than the stop level. A stop cannot fill if the stock is locked at its lower circuit; the system raises an alert and retries the next session. A time stop of 250 sessions acts as a safety net, and each strategy has its own `max_hold`.

## Research mode vs paper trading
A historical simulation cannot model the human review that a manual reset needs. So in **research backtests** the hard limits (kill switch and suspension) are **logged as "would have triggered", not enforced**. All soft limits, sizing and regime gating stay active. The frequency of would-have-triggered events is a risk statistic in its own right. In **paper trading** every hard limit is **enforced**.

## Statistical risk measures reported
Volatility, max drawdown, recovery period, VaR and CVaR (95%, daily), Monte Carlo 1-year loss probability and 95th-percentile drawdown, and the trade-order Monte Carlo losing-streak distribution.

## Prohibited by design
Martingale or doubling after losses, averaging down, unlimited sizing, leverage, short selling, derivatives, and real order placement.
