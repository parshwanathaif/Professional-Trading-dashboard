"""Persistent paper-trading ledger (SQLite). Every write for a session happens in ONE transaction,
so a crash mid-session leaves no partial records and the session is simply reprocessed.
Unique constraints make re-runs idempotent (no duplicated signals, fills or trades)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from ..logging_setup import now_ist

SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolios (
  name TEXT PRIMARY KEY, created_ist TEXT, initial_capital REAL, config_hash TEXT, test_mode INTEGER,
  notice TEXT);
CREATE TABLE IF NOT EXISTS processed_sessions (
  portfolio TEXT, session_date TEXT, run_id TEXT, processed_at_ist TEXT, config_hash TEXT, data_fingerprint TEXT,
  PRIMARY KEY (portfolio, session_date));
CREATE TABLE IF NOT EXISTS state_snapshots (
  portfolio TEXT, session_date TEXT, state_json TEXT, saved_at_ist TEXT, PRIMARY KEY (portfolio, session_date));
CREATE TABLE IF NOT EXISTS signals (
  portfolio TEXT, signal_date TEXT, strategy TEXT, symbol TEXT, side TEXT, decision TEXT, detail TEXT,
  reason TEXT, score REAL, close REAL, indicators_json TEXT, sizing_json TEXT, regime TEXT, order_id TEXT,
  test_mode INTEGER, UNIQUE (portfolio, signal_date, strategy, symbol, side));
CREATE TABLE IF NOT EXISTS order_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, portfolio TEXT, order_id TEXT, signal_date TEXT, symbol TEXT,
  strategy TEXT, side TEXT, qty INTEGER, reason TEXT, status TEXT, exec_date TEXT, detail TEXT, attempt INTEGER,
  test_mode INTEGER, UNIQUE (portfolio, order_id, status, exec_date, attempt));
CREATE TABLE IF NOT EXISTS fills (
  fill_id TEXT PRIMARY KEY, portfolio TEXT, date TEXT, symbol TEXT, side TEXT, qty INTEGER, ref_price REAL,
  fill_price REAL, value REAL, charges REAL, charges_json TEXT, exec_cost_inr REAL, order_id TEXT, strategy TEXT,
  reason TEXT, signal_date TEXT, test_mode INTEGER);
CREATE TABLE IF NOT EXISTS trades (
  trade_id TEXT PRIMARY KEY, portfolio TEXT, symbol TEXT, strategy TEXT, sector TEXT, signal_date TEXT,
  entry_date TEXT, exit_date TEXT, qty INTEGER, entry_price REAL, exit_price REAL, gross_pnl REAL, net_pnl REAL,
  charges REAL, exec_cost REAL, dividends REAL, return_pct REAL, holding_days INTEGER, entry_reason TEXT,
  exit_reason TEXT, entry_info_json TEXT, test_mode INTEGER);
CREATE TABLE IF NOT EXISTS equity (
  portfolio TEXT, date TEXT, equity REAL, cash REAL, invested REAL, exposure REAL, n_positions INTEGER,
  cum_charges REAL, cum_exec_cost REAL, cum_dividends REAL, gross_equity REAL, regime TEXT, kill_switch INTEGER,
  PRIMARY KEY (portfolio, date));
CREATE TABLE IF NOT EXISTS positions_eod (
  portfolio TEXT, date TEXT, symbol TEXT, strategy TEXT, qty INTEGER, entry_date TEXT, entry_price REAL,
  last_price REAL, market_value REAL, unrealised_pnl REAL, stop REAL, sector TEXT, PRIMARY KEY (portfolio, date, symbol));
CREATE TABLE IF NOT EXISTS risk_events (
  portfolio TEXT, date TEXT, severity TEXT, code TEXT, detail TEXT, strategy TEXT,
  UNIQUE (portfolio, date, code, detail));
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, portfolio TEXT, ts_ist TEXT, event TEXT, payload_json TEXT);
"""


class PaperLedger:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        try:
            con.execute("BEGIN")
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def query(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        con = sqlite3.connect(self.path, timeout=30)
        try:
            return pd.read_sql_query(sql, con, params=params)
        finally:
            con.close()

    # ------------------------------------------------------------------ portfolio
    def ensure_portfolio(self, name: str, capital: float, config_hash: str, test_mode: bool, notice: str) -> None:
        with self.tx() as con:
            con.execute("INSERT OR IGNORE INTO portfolios VALUES (?,?,?,?,?,?)",
                        (name, now_ist().isoformat(timespec="seconds"), capital, config_hash, int(test_mode), notice))

    def last_session(self, portfolio: str) -> str | None:
        df = self.query("SELECT MAX(session_date) AS d FROM processed_sessions WHERE portfolio=?", (portfolio,))
        return df["d"].iloc[0] if len(df) and df["d"].iloc[0] else None

    def load_snapshot(self, portfolio: str) -> tuple[str | None, dict | None]:
        df = self.query("SELECT session_date, state_json FROM state_snapshots WHERE portfolio=? "
                        "ORDER BY session_date DESC LIMIT 1", (portfolio,))
        if df.empty:
            return None, None
        return df["session_date"].iloc[0], json.loads(df["state_json"].iloc[0])

    def audit(self, con, portfolio: str, event: str, payload: dict) -> None:
        con.execute("INSERT INTO audit_log (portfolio, ts_ist, event, payload_json) VALUES (?,?,?,?)",
                    (portfolio, now_ist().isoformat(timespec="seconds"), event, json.dumps(payload, default=str)))

    # ------------------------------------------------------------------ session commit
    def commit_session(self, portfolio: str, session_date: str, run_id: str, config_hash: str, fingerprint: str,
                       state: dict, signals: list[dict], orders: list[dict], fills: list[dict], trades: list[dict],
                       equity_row: dict, positions: list[dict], risk_events: list[dict]) -> None:
        with self.tx() as con:
            cur = con.execute("SELECT 1 FROM processed_sessions WHERE portfolio=? AND session_date=?",
                              (portfolio, session_date))
            if cur.fetchone():
                raise RuntimeError(f"session {session_date} already processed for {portfolio}")
            for s in signals:
                con.execute(
                    "INSERT OR IGNORE INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (portfolio, s["date"], s["strategy"], s["symbol"], s["side"], s["decision"], s.get("detail"),
                     s.get("reason"), s.get("score"), s.get("close"), json.dumps(s.get("indicators", {})),
                     json.dumps(s.get("sizing", {})), s.get("regime"), s.get("order_id"), int(s.get("test_mode", 0))))
            for o in orders:
                con.execute(
                    "INSERT OR IGNORE INTO order_events (portfolio, order_id, signal_date, symbol, strategy, side, qty, "
                    "reason, status, exec_date, detail, attempt, test_mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (portfolio, o["order_id"], o["signal_date"], o["symbol"], o["strategy"], o["side"], o["qty"],
                     o["reason"], o["status"], o.get("exec_date") or "", o.get("detail"), o.get("attempt", 0),
                     int(o.get("test_mode", 0))))
            for f in fills:
                con.execute(
                    "INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f["fill_id"], portfolio, f["date"], f["symbol"], f["side"], f["qty"], f["ref_price"],
                     f["fill_price"], f["value"], f["charges"], json.dumps(f["charges_detail"]), f["exec_cost_inr"],
                     f["order_id"], f["strategy"], f["reason"], f["signal_date"], int(f.get("test_mode", 0))))
            for t in trades:
                con.execute(
                    "INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (t["trade_id"], portfolio, t["symbol"], t["strategy"], t["sector"], t["signal_date"],
                     t["entry_date"], t["exit_date"], t["qty"], t["entry_price"], t["exit_price"], t["gross_pnl"],
                     t["net_pnl"], t["charges"], t["exec_cost"], t["dividends"], t["return_pct"], t["holding_days"],
                     t["entry_reason"], t["exit_reason"], json.dumps(t.get("entry_info", {})),
                     int(t.get("test_mode", 0))))
            e = equity_row
            con.execute("INSERT OR REPLACE INTO equity VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (portfolio, session_date, e["equity"], e["cash"], e["invested"], e["exposure"],
                         e["n_positions"], e["cum_charges"], e["cum_exec_cost"], e["cum_dividends"],
                         e["gross_equity"], e["regime"], int(e["kill_switch"])))
            for p in positions:
                con.execute("INSERT OR REPLACE INTO positions_eod VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (portfolio, session_date, p["symbol"], p["strategy"], p["qty"], p["entry_date"],
                             p["entry_price"], p["last_price"], p["market_value"], p["unrealised_pnl"], p["stop"],
                             p["sector"]))
            for r in risk_events:
                con.execute("INSERT OR IGNORE INTO risk_events VALUES (?,?,?,?,?,?)",
                            (portfolio, r["date"], r["severity"], r["code"], r["detail"], r.get("strategy")))
            con.execute("INSERT OR REPLACE INTO state_snapshots VALUES (?,?,?,?)",
                        (portfolio, session_date, json.dumps(state, default=str), now_ist().isoformat(timespec="seconds")))
            con.execute("INSERT INTO processed_sessions VALUES (?,?,?,?,?,?)",
                        (portfolio, session_date, run_id, now_ist().isoformat(timespec="seconds"), config_hash, fingerprint))
            self.audit(con, portfolio, "SESSION_PROCESSED",
                       {"session": session_date, "run_id": run_id, "signals": len(signals), "fills": len(fills),
                        "trades": len(trades), "equity": e["equity"]})

    def update_risk_state(self, portfolio: str, risk_state: dict, event: dict) -> None:
        with self.tx() as con:
            row = con.execute("SELECT session_date, state_json FROM state_snapshots WHERE portfolio=? "
                              "ORDER BY session_date DESC LIMIT 1", (portfolio,)).fetchone()
            if row is None:
                raise RuntimeError("no paper-trading state to reset")
            state = json.loads(row[1])
            state["risk"] = risk_state
            con.execute("UPDATE state_snapshots SET state_json=?, saved_at_ist=? WHERE portfolio=? AND session_date=?",
                        (json.dumps(state, default=str), now_ist().isoformat(timespec="seconds"), portfolio, row[0]))
            con.execute("INSERT OR IGNORE INTO risk_events VALUES (?,?,?,?,?,?)",
                        (portfolio, event["date"], event["severity"], event["code"], event["detail"], event.get("strategy")))
            self.audit(con, portfolio, event["code"], event)
