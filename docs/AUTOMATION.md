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
