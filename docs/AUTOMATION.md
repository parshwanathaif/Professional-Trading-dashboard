# Automated Paper Trading on GitHub Actions

Workflow: `.github/workflows/paper-trading.yml`. **Paper trading only.** There is no broker connection, and no real order can be placed.

## Schedule (IST)
| When | What |
|---|---|
| Mon–Fri 19:15 | Daily run: fetch the day's official NSE/BSE archive files, then data-quality gate, regimes and signals, the independent risk engine, simulated fills at the next open, mark-to-market and daily report |
| Mon–Fri 22:00 | Final attempt, in case the files were published late. It is idempotent: a session that has already been processed is skipped. |
| Saturday 10:00 | Weekly review: strategy comparison, exposure, drawdown, regimes and degradation monitoring. During days 1–7 of the month it also runs the **monthly reassessment** (full research re-run, walk-forward and parameter stability), which can change which strategies may trade. |

GitHub may delay scheduled runs by several minutes during busy periods. Scheduled workflows in a repository with no activity for 60 days are paused by GitHub; a commit or a manual run re-enables them.

## Manual runs
Actions → *Automated paper trading* → **Run workflow**, then choose a task:
- `daily` (optionally with `as_of` = a session date)
- `weekly`
- `monthly`
- `research`
- `seed`: download the full history only. This is needed once; it takes about two hours.

## Daily trading calls (BUY / HOLD / SELL)
After each daily run that processes a new session, the workflow publishes **"Trading calls for <next session>"** as a GitHub issue labelled `trading-calls`. As the repository owner you are notified by email and in the GitHub mobile app; to make sure, set *Watch → All activity* on the repository. The previous day's calls issue is closed automatically.

Each issue contains:
- **BUY.** Stock, suggested weight (% of capital), quantity for the capital in `paper_trading.calls.your_capital_inr`, last close, **stop-loss** price and %, the strategy and the reason. Buy at the next open and place the stop-loss immediately.
- **SELL.** Stock, entry, last close, P&L and the exit reason (signal exit, time stop). Sell at the next open.
- **Already exited today.** Positions whose stop-loss was hit during the session.
- **HOLD.** Open positions with their current stop-loss, P&L and weight.
- **Live track record.** Return, drawdown, hit rate and average result of all closed calls since the model portfolio started, after all simulated costs.
- **Strategies selected and their out-of-sample evidence.**

### How the strategy is chosen
`paper_trading.strategy_selection.mode: best_evidence` selects the top `max_strategies` (default 2) by the research **score**. The score combines out-of-sample and walk-forward results, drawdown, consistency, cost sensitivity, parameter stability, regime robustness, liquidity and the Deflated Sharpe Ratio. Only strategies whose status is at least `min_status` (default "Needs further validation") qualify, and **a Rejected strategy is never used.** The monthly reassessment recomputes this automatically.

If no strategy qualifies, the issue says so and contains **no BUY calls**. Open positions keep being managed with HOLD and SELL calls.

### Shadow book (owner decision, 2026-11-24)
No strategy has yet **passed** historical validation on real NSE data, so the main (real-use) book requires "Passed historical validation" (`strategy_selection.min_status`) and currently issues no BUY calls. The owner chose **"shadow calls first"**: a separate portfolio (`paper_trading.shadow`, book `shadow_trend`) follows the highest research-score strategy that passed the look-ahead test, currently 50/250 trend following ("Needs further validation"). Its calls appear in the same daily issue under **SHADOW calls - TRACK ONLY**, with the shadow book's own live record. Every shadow decision is tagged `test_mode=1` in the audit trail. Decide on real-money use only after that live record supports it. Set `shadow.enabled: false` to stop it.

### Mirroring the calls with a broker
The calls assume entry at the next session's open and a protective stop-loss kept at the broker. Your fills will differ from the model's simulated fills. The model portfolio's track record is the reference.

## Where state lives
- The **Actions cache** holds `data/raw/exchange`, `data/processed`, `state` and `reports`. Only workflows of this repository can read it, so exchange data are never committed or published. The latest cache is restored at the start of each run and a new one is saved at the end.
- If the cache is ever evicted (GitHub evicts caches unused for 7 days, and the total limit is 10 GB), the next run re-downloads the history and resumes automatically. **The paper ledger would restart in that case.** Download the `reports-<run>` artifact regularly if you need a permanent record.
- Run artifacts (`reports-<run_id>`, kept for 90 days) contain the paper reports (Markdown, HTML, CSV, Excel, PDF), research summaries and reports, and data-quality summaries. They contain no raw exchange files.

## Alerts
A GitHub issue titled **"Paper-trading automation needs attention"** is opened, or commented on if one is already open, when:
- a workflow step fails;
- the final 22:00 attempt still finds no data or the data fail quality checks (possibly an exchange holiday missing from the holiday list);
- the **kill switch** is active, or a strategy is suspended.

Every run also writes a summary (portfolio, research statuses, alerts) on its Actions run page.

## Manual reset after a kill switch
The kill switch requires a human decision. Reset it locally with the same state, or add a one-off step to the workflow:

```bash
python main.py paper reset-kill-switch --operator "Your Name" --note "Reason and actions taken"
```

## Why no trades may appear
New paper entries are allowed only for strategies whose latest research status is **"Eligible for paper-trading evaluation"**. If no strategy passes validation on the real NSE history, the automation keeps running every day (fetching data, checking quality, monitoring regimes and reporting) but opens no positions. That is the risk framework doing its job. Lowering `paper_trading.allowed_statuses`, or enabling `test_mode_allow_unvalidated`, is possible. Those decisions are tagged `TEST_MODE` in the audit trail and carry no statistical support.
