# Paper-Trading Operating Manual

> All orders are simulated inside the local engine. There is no broker connection and none can be configured.

## Lifecycle
1. **Research first.** Run `python main.py research`. A strategy can open paper positions only if its latest status is in `paper_trading.allowed_statuses`. The default is "Eligible for paper-trading evaluation".
2. **Start the portfolio.**
   - `python main.py paper run` starts it from the latest validated session.
   - `python main.py paper run --start 2025-01-01` backfills a new portfolio session by session.
3. **Daily.** After the close (the default run time is 18:30 IST), the daily workflow imports new files from `data/raw/inbox`, validates the data, checks completeness, fills yesterday's orders at today's open, processes stops and dividends, marks positions, applies risk checks, generates tomorrow's orders and writes the daily report.
4. **Weekly and monthly.** Weekly runs a review with degradation monitoring. Monthly re-runs the full research process, which can change which strategies are allowed.

## Session processing guarantees
- Each session is committed in **one SQLite transaction**: signals, order events, fills, trades, equity, positions, risk events, the state snapshot, the processed-session marker and the audit entry.
- A crash before the commit leaves nothing behind, and the next run reprocesses that session from the previous snapshot.
- Re-running a processed session is impossible: `processed_sessions` has a primary key, and signals, fills and trades have unique keys. `paper run` on an up-to-date portfolio returns `UP_TO_DATE`.

## Audit trail (tables in `state/paper_trading.sqlite`)
| Table | Contents |
|---|---|
| `signals` | Date, strategy, symbol, reason text, score, close, indicator values (JSON), regime, decision (ORDER/REJECTED), rejection reason, sizing detail and binding constraint, order_id |
| `order_events` | Each order's lifecycle: CREATED, FILLED, PARTIAL, CIRCUIT, NO_BAR, NO_VOLUME, REJECTED, CANCELLED |
| `fills` | Reference price, fill price, value, every charge component, execution cost, order and signal linkage |
| `trades` | Entry and exit dates and prices, gross and net P&L, charges, dividends, holding days, entry and exit reasons, entry indicator values |
| `equity`, `positions_eod` | Daily marks, unrealised P&L, stops |
| `risk_events` | Loss-limit breaches, drawdown warnings, kill switch, suspensions, gap alerts, blocked stops |
| `audit_log` | Session commits, manual resets |

## Kill switch & suspensions
When the portfolio drawdown breaches `max_drawdown_kill` (20%), new entries stop immediately and stay stopped across restarts. Exits keep working. To resume after a review:

```bash
python main.py paper status
```

```bash
python main.py paper reset-kill-switch --operator "Your Name" --note "Reason for reset and actions taken"
```

A strategy suspended after consecutive losses is reset with:

```bash
python main.py paper reset-strategy --strategy trend_ma --operator "Your Name" --note "Reviewed"
```

Every reset is written to `risk_events` and `audit_log`.

## Test mode
`paper_trading.test_mode_allow_unvalidated: true` lets unvalidated strategies trade. It is **for software testing only** (the demo uses it). Every signal, order, fill and trade is tagged `test_mode = 1`, and the dashboard shows a warning.

## Data stops
Paper trading refuses to process a session when the data-quality gate fails, when the benchmark is missing for the session, or when fewer than 90% of the previous session's symbols have data. The workflow status shows the reason. Fix the data, then re-run.
