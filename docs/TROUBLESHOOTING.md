# Troubleshooting, Scheduling, Backup & Recovery

## Common problems
| Symptom | Cause | Fix |
|---|---|---|
| `data-quality gate failed` | Dataset-level CRITICAL issue (usually the benchmark is missing) | Open `reports/data_quality/*.html`. Import the benchmark named in `backtest.benchmark`. |
| Many symbols excluded | Invalid OHLC, long gaps, or conflicting duplicates | Check the issues CSV and the `quarantine` table. Fix at source and re-import. |
| Workflow `STOPPED: data for … not yet available` | Today's files have not arrived | Drop the files in `data/raw/inbox`. The next scheduled run continues. |
| `COMPLETED_NO_SESSION` | Weekend or holiday | None needed |
| `workflow already running` | Another run holds the lock | Wait. A lock older than `lock_stale_after_minutes` is taken over automatically. |
| Paper run `incomplete data` | Fewer than 90% of symbols present for the session | Complete the import, then re-run `python main.py schedule daily --as-of YYYY-MM-DD`. |
| No paper entries | No strategy has an allowed status, or the kill switch or a regime block is active | Check `python main.py paper status` and the dashboard Risk page. |
| `Conversion Error` on import | Wrong column type | Check the column reference in DATA_IMPORT_GUIDE.md |
| Status capped at "Insufficient evidence" / "Needs further validation" | Synthetic data, or a universe that is not point-in-time | Import licensed point-in-time membership and delisted securities |

## Scheduling (cron, IST host)

```bash
crontab -e
```

Add these lines. `cd` to your install path first, and adjust it.

```
45 18 * * 1-5  cd /opt/bharat_quant_lab && .venv/bin/python main.py schedule daily   >> logs/cron.log 2>&1
15 9  * * 6    cd /opt/bharat_quant_lab && .venv/bin/python main.py schedule weekly  >> logs/cron.log 2>&1
30 10 1-7 * 6  cd /opt/bharat_quant_lab && .venv/bin/python main.py schedule monthly >> logs/cron.log 2>&1
```

If the host is not on IST, set `CRON_TZ=Asia/Kolkata` at the top of the crontab. Alternatively, run the built-in loop (for example under systemd):

```bash
python main.py schedule daemon
```

Re-running a workflow is safe. A completed key is skipped. Use `--force` to re-run it deliberately; steps are idempotent.

## Restart after failure
A failed workflow resumes from the step after the last completed one. Look at `state/last_run_status.json`, fix the cause, and re-run the same command.

## Backup & recovery

```bash
tar czf backup_$(date +%Y%m%d).tgz config data/processed state reports/research
```

To restore, stop the scheduler, extract the archive into the project directory, and run `python main.py validate-data` and `python main.py paper status`. The paper ledger is self-consistent at every committed session.

## Logs
`logs/bql_YYYYMMDD.jsonl` is a structured JSON log with IST timestamps. Every import, data-quality result, workflow step, paper session and research result is logged.
