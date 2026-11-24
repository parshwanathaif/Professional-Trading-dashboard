# Strategy Documentation

All strategies are **long-only**, use **daily closes**, and are **causal**: row *t* uses only data up to the close of *t*, which the leakage test verifies. Orders execute at the next session's open. Sizing, stops and limits come from the risk engine, not the strategy. Default stop: 2.5 × ATR(20) below the fill price.

Notation: `P_t` adjusted close, `H/L` high and low, `V` volume, `SMA_n` simple moving average, `σ` return volatility, `ATR_n` Wilder average true range.

---

## 1. `trend_ma` — Moving-average trend following (Trend Following)
**Hypothesis.** Prices above a rising long-term average tend to keep trending (time-series momentum, from investor under-reaction and herding).

**Plain English.** Buy when the 50-day average is above the 200-day average and the price is above the 50-day average. Sell when the 50-day average drops below the 200-day average or the price closes below the 200-day average. If there are more candidates than capital, prefer the ones furthest above their 200-day average relative to their volatility.

**Mathematics.**
`entry_t = 1[SMA_f(P)_t > SMA_s(P)_t ∧ P_t > SMA_f(P)_t]`,
`exit_t = 1[SMA_f(P)_t < SMA_s(P)_t ∨ P_t < SMA_s(P)_t]`,
`score_t = (P_t / SMA_s,t − 1) / σ_63,t`.
Grid (training only): fast ∈ {20, 50, 100}, slow ∈ {150, 200, 250}.

**Expected failure modes.** Whipsaws in sideways markets, and late exits in fast crashes (gap risk).

## 2. `xs_momentum` — Cross-sectional risk-adjusted momentum (Momentum)
**Hypothesis.** Stocks with the strongest 6–12 month returns, skipping the latest month, tend to outperform over the next months (Jegadeesh–Titman). Volatility adjustment reduces crash risk.

**Plain English.** Once a month, rank eligible stocks by their return from 12 months ago to 1 month ago divided by their volatility. Buy the top N that also have positive momentum. At the next monthly review, sell any holding whose rank has fallen below 2×N or whose momentum has turned negative.

**Mathematics.** `MOM_t = P_{t−skip}/P_{t−L} − 1`; `score_t = MOM_t / σ_126,t`; `rank_t` = cross-sectional rank among the eligible universe; on rebalance days `entry = rank ≤ N ∧ MOM > 0`, `exit = rank > 2N ∨ MOM ≤ 0`.
Grid: L ∈ {126, 189, 252}, N ∈ {5, 10, 15}.

**Expected failure modes.** Momentum crashes at sharp market reversals (2009 and 2020 style). Sector concentration, which is mitigated by the 25% sector cap.

## 3. `rsi_mean_reversion` — Short-term RSI reversion in uptrends (Mean Reversion)
**Hypothesis.** Short, sharp sell-offs in stocks that are in long-term uptrends partly reverse within days, as liquidity providers are rewarded.

**Plain English.** Buy when the 3-day RSI falls below 10 while the price is above its 200-day average. Sell when the RSI recovers above 60 or after 10 sessions.

**Mathematics.** `RSI_n` uses Wilder smoothing. `entry = RSI_n < a ∧ P > SMA_200`, `exit = RSI_n > b ∨ held ≥ max_hold`, `score = −RSI_n`.
Grid: n ∈ {2, 3, 5}, a ∈ {5, 10, 20}.

**Expected failure modes.** Very sensitive to costs, because of high turnover and the ~0.25% statutory round trip. Catches falling knives in regime breaks.

## 4. `bollinger_reversion` — Bollinger-band reversion (Mean Reversion)
**Hypothesis.** A close below the lower band in an uptrend tends to revert towards the middle band.

**Plain English.** Buy when the price closes below its 20-day average minus 2 standard deviations, provided it is above its 200-day average. Sell when it closes back above the 20-day average or after 15 sessions.

**Mathematics.** `mid = SMA_n(P)`, `lower = mid − k·sd_n(P)`; `entry = P < lower ∧ P > SMA_200`, `exit = P > mid`, `score = (mid − P)/sd`.
Grid: n ∈ {10, 20, 30}, k ∈ {1.5, 2.0, 2.5}.

## 5. `donchian_breakout` — Volume-confirmed channel breakout (Breakout)
**Hypothesis.** A close above the prior N-day high on above-average volume shows new demand, and that demand tends to persist.

**Plain English.** Buy when the close exceeds the highest high of the previous 55 days and volume is more than 1.5× its 50-day average. Sell when the close falls below the lowest low of the previous 20 days.

**Mathematics.** `HH_t = max(H_{t−N..t−1})`, `LL_t = min(L_{t−M..t−1})`; `entry = P_t > HH_t ∧ V_t > m·SMA_50(V)_{t−1}`, `exit = P_t < LL_t`, `score = (P_t − HH_t)/ATR_t`.
Grid: N ∈ {20, 55, 100}, m ∈ {1.0, 1.5, 2.0}.

## 6. `vol_breakout` — Volatility squeeze then expansion (Volatility)
**Hypothesis.** Unusually low volatility is followed by expansion. An upside range expansion in an uptrend marks the start of a directional move.

**Plain English.** A stock qualifies if, within the last 5 sessions, its ATR as a percentage of price was in the bottom 25% of its last 120 days. Buy when today's range exceeds 1.5× the prior ATR, the close is above the prior 20-day high, and the close is above the 100-day average. Sell on a close below the prior 20-day low.

**Mathematics.** `NATR = ATR_14/P`, `pct_t` = rolling percentile of NATR over W, `squeeze_t = min(pct_{t−5..t−1}) ≤ q`; `entry = squeeze ∧ TR_t > 1.5·ATR_{t−1} ∧ P_t > max(H_{t−20..t−1}) ∧ P_t > SMA_trend`.
Grid: q ∈ {0.15, 0.25, 0.35}, trend ∈ {50, 100, 200}.

---

## Volatility controls & market regime detection (families E and F)
ATR risk control, volatility-adjusted sizing and portfolio volatility targeting live in the **risk engine**, so every strategy gets them. **Regime detection** (`src/bql/regime/detector.py`) classifies each day from the benchmark as STRONG/WEAK BULLISH, SIDEWAYS, WEAK/STRONG BEARISH, HIGH_VOLATILITY or MARKET_STRESS, with a LOW_VOLATILITY flag. It uses the 200-day trend, the slope of the 200-day average, the 50-day average, and the **expanding** percentile of 21-day realised volatility (each day is ranked only against its own past), plus drawdown from the 252-day high. It is used for:
- entry gating per strategy (`regime_gating` in strategies.yaml);
- regime exposure caps (risk_limits.yaml);
- regime performance breakdowns in every evaluation report.

## Portfolio strategies (family G)
`bql.portfolio.combine_strategies` combines the strategies' walk-forward out-of-sample return streams using equal weight, inverse volatility or equal risk contribution (risk parity). Weights are re-estimated monthly from past data only, with a 40% per-strategy cap. A diversification report (correlation matrix, diversification ratio) is produced with every research run. To run several strategies in one shared book with shared risk limits, pass several strategies to `run_backtest`. The paper trader always does this.

## Adding a strategy
1. Subclass `Strategy` in `src/bql/strategies/library.py`. Implement `_generate(panel, universe) -> Signals` using only causal operations, and document the hypothesis, description and mathematics.
2. Register it in `STRATEGIES` and add it to `config/strategies.yaml` with a small grid.
3. `pytest tests/test_strategies_backtest.py` runs the causality test on every registered strategy automatically.
