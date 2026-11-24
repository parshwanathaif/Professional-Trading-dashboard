#!/usr/bin/env python
"""Summarise an automated run for GitHub Actions and decide whether to raise an alert.

Writes Markdown to stdout (appended to $GITHUB_STEP_SUMMARY by the workflow) and writes
`state/alert.md` when operator attention is needed:
  * workflow FAILED
  * workflow STOPPED on the final (late-evening) attempt - data missing or failed quality checks
  * kill switch active, or a strategy suspended
Exit code is 0 unless the workflow FAILED (so the job is marked failed and visible).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bql import PAPER_ONLY_NOTICE  # noqa: E402
from bql.config import load_config  # noqa: E402


def _book(db, name, label, lines, alerts) -> None:
    import sqlite3

    if not db.exists():
        return
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT date, equity, cash, invested, exposure, n_positions, kill_switch FROM equity "
                          "WHERE portfolio=? ORDER BY date DESC LIMIT 1", (name,)).fetchone()
        snap = con.execute("SELECT state_json FROM state_snapshots WHERE portfolio=? ORDER BY session_date DESC "
                           "LIMIT 1", (name,)).fetchone()
        trades = con.execute("SELECT COUNT(*), COALESCE(SUM(net_pnl),0) FROM trades WHERE portfolio=?",
                             (name,)).fetchone()
    finally:
        con.close()
    if row:
        lines += ["", f"## {label}", "",
                  "| Session | Equity (net, INR) | Cash | Invested | Exposure | Positions | Closed trades | Realised P&L |",
                  "|---|---|---|---|---|---|---|---|",
                  f"| {row[0]} | {row[1]:,.0f} | {row[2]:,.0f} | {row[3]:,.0f} | {row[4]:.0%} | {row[5]} | "
                  f"{trades[0]} | {trades[1]:,.0f} |"]
    if snap:
        risk = json.loads(snap[0])["risk"]
        if risk.get("kill_switch"):
            alerts.append(f"[{name}] KILL SWITCH active since {risk.get('kill_date')}: {risk.get('kill_reason')}. "
                          "New simulated entries are blocked until a manual reset.")
        if risk.get("suspended"):
            alerts.append(f"[{name}] Strategies suspended: {risk.get('suspended')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--final-attempt", action="store_true", help="last scheduled attempt of the day")
    ap.add_argument("--job-status", default="success", help="GitHub job status so far (success/failure/cancelled)")
    args = ap.parse_args()
    cfg = load_config()
    state = cfg.path("state_dir")
    lines = ["# Bharat Quant Lab - automated run", "", f"> {PAPER_ONLY_NOTICE}", ""]
    alerts: list[str] = []
    status_file = state / "last_run_status.json"
    status = json.loads(status_file.read_text()) if status_file.exists() else {}
    if status:
        lines += [f"**Workflow:** `{status.get('workflow')}` key `{status.get('key')}` -> **{status.get('status')}** "
                  f"({status.get('finished_ist')})", ""]
        lines += [f"- {m}" for m in status.get("messages", [])]
        if status.get("error"):
            lines += ["", "```", str(status["error"])[:3000], "```"]
        if status.get("status") == "FAILED":
            alerts.append(f"Workflow `{status.get('key')}` FAILED: {str(status.get('error'))[:500]}")
        if status.get("status") == "STOPPED" and args.final_attempt:
            alerts.append(f"Workflow `{status.get('key')}` STOPPED on the final attempt of the day "
                          f"(no paper trades were generated): {str(status.get('error'))[:500]}. "
                          "If today was an exchange holiday, add it to the holiday list and close this issue.")
    if args.job_status != "success" and status.get("status") != "FAILED":
        alerts.append(f"A workflow step failed outside the daily workflow (job status: {args.job_status}). "
                      "Open the run log for the failing step.")
    db = cfg.path("paper_db")
    books = [(cfg.settings.paper_trading.portfolio_name, "Paper portfolio (validated strategies)")]
    if cfg.settings.paper_trading.shadow.enabled:
        books.append((cfg.settings.paper_trading.shadow.portfolio_name, "SHADOW book (validation-rejected, track only)"))
    for name, label in books:
        _book(db, name, label, lines, alerts)
    latest = cfg.path("reports_dir") / "research" / "latest.json"
    if latest.exists():
        meta = json.loads(latest.read_text())
        allowed = cfg.settings.paper_trading.allowed_statuses
        lines += ["", f"## Research statuses (run `{meta.get('run_id')}`)", "", "| Strategy | Status | Paper entries |",
                  "|---|---|---|"]
        for k, v in meta.get("statuses", {}).items():
            lines.append(f"| {k} | {v} | {'allowed' if v in allowed else 'blocked'} |")
    if alerts:
        lines += ["", "## Alerts", ""] + [f"- {a}" for a in alerts]
        (state / "alert.md").write_text("\n".join(["Automated paper-trading run needs attention.", ""] +
                                                  [f"- {a}" for a in alerts]))
    else:
        (state / "alert.md").unlink(missing_ok=True)
    print("\n".join(lines))
    return 1 if status.get("status") == "FAILED" else 0


if __name__ == "__main__":
    sys.exit(main())
