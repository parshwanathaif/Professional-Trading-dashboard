"""Daily BUY / HOLD / SELL calls derived from the model (paper) portfolio.

The model portfolio is the paper-trading book. Each evening, after the session has been
processed, the generator reads:
  * pending BUY orders  -> BUY calls for the next session's open (with size and stop-loss)
  * pending SELL orders -> SELL calls for the next session's open (with the exit reason)
  * stop-loss exits that happened today -> EXITED notices
  * open positions not being sold -> HOLD calls (with the current stop-loss)
plus the live track record of all calls so far and the evidence behind the strategies in use.

Nothing here places an order. These are research outputs for the owner's own decisions.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd

from .. import PAPER_ONLY_NOTICE
from ..config import AppConfig
from ..logging_setup import now_ist
from ..market_calendar import TradingCalendar

DISCLAIMER = (
    "Generated automatically by a rule-based model from NSE end-of-day data. Not investment advice and "
    "no guarantee of profit: every strategy can and does lose money. Calls are for the repository owner's "
    "own use; distributing buy/sell recommendations to others in India requires SEBI Research Analyst "
    "registration."
)


def _q(db: Path, sql: str, params: tuple = ()) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return pd.read_sql_query(sql, con, params=params)
    finally:
        con.close()


def _evidence(cfg: AppConfig) -> dict[str, dict]:
    root = cfg.path("reports_dir") / "research"
    latest = root / "latest.json"
    if not latest.exists():
        return {}
    run_id = json.loads(latest.read_text())["run_id"]
    out = {}
    for ev_path in (root / run_id).glob("*/evaluation.json"):
        e = json.loads(ev_path.read_text())
        wf, t = e.get("walk_forward", {}), e.get("test", {})
        out[e["strategy"]] = {
            "family": e.get("family"), "status": e.get("status"), "score": e.get("score", {}).get("total"),
            "description": e.get("describe", {}).get("description"),
            "wf_oos_sharpe": wf.get("oos_sharpe"), "wf_oos_cagr": wf.get("oos_cagr"),
            "test_cagr_net": t.get("cagr_net"), "test_max_drawdown": t.get("max_drawdown"),
            "test_win_rate": t.get("win_rate"), "test_trades": t.get("trades"),
            "test_period": e.get("splits", {}).get("test"),
        }
    return out


def build_calls(cfg: AppConfig, allowed: dict[str, bool] | None = None, shadow: bool = False) -> dict:
    db = cfg.path("paper_db")
    pc = cfg.settings.paper_trading
    name = pc.shadow.portfolio_name if shadow else pc.portfolio_name
    snap = _q(db, "SELECT session_date, state_json FROM state_snapshots WHERE portfolio=? "
                  "ORDER BY session_date DESC LIMIT 1", (name,))
    if snap.empty:
        raise RuntimeError("no model-portfolio state yet: run the daily workflow first")
    session = snap["session_date"].iloc[0]
    state = json.loads(snap["state_json"].iloc[0])
    eq = _q(db, "SELECT * FROM equity WHERE portfolio=? ORDER BY date", (name,))
    equity = float(eq["equity"].iloc[-1])
    my_cap = cfg.settings.paper_trading.calls.your_capital_inr
    from ..data import MarketStore

    cal = TradingCalendar.from_store(MarketStore(cfg.path("market_db"), read_only=True))
    next_session = str(cal.next_trading_day(session).date())
    sigs = _q(db, "SELECT * FROM signals WHERE portfolio=? AND signal_date=? AND decision='ORDER'", (name, session))
    sig_by_order = {r.order_id: r for r in sigs.itertuples()} if not sigs.empty else {}
    evidence = _evidence(cfg)

    buys, sells = [], []
    for o in state["pending"]:
        if o["side"] == "BUY":
            s = sig_by_order.get(o["order_id"])
            ref = float(s.close) if s is not None else float("nan")
            weight = o["qty"] * ref / equity if equity > 0 else float("nan")
            stop = ref - float(o["stop_distance"]) if o.get("stop_distance") == o.get("stop_distance") else None
            buys.append({
                "symbol": o["symbol"], "strategy": o["strategy"], "action": "BUY",
                "when": f"at the open on {next_session}", "last_close": round(ref, 2),
                "weight_pct": round(100 * weight, 2),
                "qty_for_your_capital": int(my_cap * weight / ref) if ref and ref == ref else None,
                "stop_loss": round(stop, 2) if stop else None,
                "stop_loss_pct": round(100 * (stop / ref - 1), 2) if stop and ref else None,
                "reason": o["reason"], "indicators": o.get("info", {}),
            })
        else:
            pos = state["positions"].get(o["symbol"], {})
            sells.append({
                "symbol": o["symbol"], "strategy": o["strategy"], "action": "SELL",
                "when": f"at the open on {next_session}", "reason": o["reason"],
                "entry_date": pos.get("entry_date"), "entry_price": round(pos.get("entry_fill_price", 0), 2),
                "last_close": round(pos.get("last_price", 0), 2),
                "unrealised_pct": round(100 * (pos.get("last_price", 0) / pos["entry_fill_price"] - 1), 2)
                if pos.get("entry_fill_price") else None,
            })
    selling = {s["symbol"] for s in sells}
    holds = []
    for sym, p in state["positions"].items():
        if sym in selling:
            continue
        holds.append({
            "symbol": sym, "strategy": p["strategy"], "action": "HOLD", "entry_date": p["entry_date"],
            "entry_price": round(p["entry_fill_price"], 2), "last_close": round(p["last_price"], 2),
            "unrealised_pct": round(100 * (p["last_price"] / p["entry_fill_price"] - 1), 2),
            "stop_loss": round(p["stop"], 2) if p.get("stop") else None,
            "weight_pct": round(100 * p["qty"] * p["last_price"] / equity, 2), "days_held": p["bars_held"],
        })
    exited = _q(db, "SELECT symbol, strategy, exit_reason, net_pnl, return_pct FROM trades WHERE portfolio=? "
                    "AND exit_date=?", (name, session))
    trades = _q(db, "SELECT net_pnl, return_pct FROM trades WHERE portfolio=?", (name,))
    track = {
        "model_portfolio_start": str(eq["date"].iloc[0]), "sessions": int(len(eq)),
        "model_equity_inr": round(equity, 0),
        "model_return_pct": round(100 * (equity / cfg.settings.paper_trading.initial_capital_inr - 1), 2),
        "closed_calls": int(len(trades)),
        "hit_rate_pct": round(100 * float((trades["net_pnl"] > 0).mean()), 1) if len(trades) else None,
        "avg_return_per_closed_call_pct": round(100 * float(trades["return_pct"].mean()), 2) if len(trades) else None,
        "max_drawdown_pct": round(100 * float((eq["equity"] / eq["equity"].cummax() - 1).min()), 2),
        "kill_switch": bool(state["risk"].get("kill_switch")),
    }
    in_use = sorted(k for k, v in (allowed or {}).items() if v)
    return {"generated_ist": now_ist().isoformat(timespec="seconds"), "session": session,
            "next_session": next_session, "buy": buys, "sell": sells, "hold": holds,
            "exited_today": exited.to_dict("records"), "track_record": track,
            "strategies_in_use": {k: evidence.get(k, {}) for k in in_use},
            "research_evidence": evidence, "regime": eq["regime"].iloc[-1], "portfolio": name, "shadow": shadow,
            "test_mode": bool(cfg.settings.paper_trading.test_mode_allow_unvalidated) and not shadow}


def _pct(x) -> str:
    try:
        return f"{100 * float(x):.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def render_markdown(c: dict, heading_level: int = 1) -> str:
    h = "#" * heading_level
    title = "SHADOW calls" if c.get("shadow") else "Validated calls"   # validated = passed the configured min status
    L = [f"{h} {title} for {c['next_session']}", "",
         f"Based on NSE close of **{c['session']}** · market regime: **{c['regime']}** · generated {c['generated_ist']}",
         ""]
    if c.get("shadow"):
        names = ", ".join(f"{k} (status: {v.get('status', 'unknown')})" for k, v in c["strategies_in_use"].items()) or "none"
        L += [f"> **SHADOW - TRACK ONLY.** Strategy: **{names}**, the highest research score, which has **not passed** "
              "strict historical validation. These calls build a live out-of-sample record. Do not commit real money "
              "until that record supports it. The track record below is the shadow book's own record since it started.",
              ""]
    if c["test_mode"]:
        L += ["> **TEST MODE** - unvalidated strategies allowed. Not supported by evidence.", ""]
    if c["track_record"]["kill_switch"]:
        L += ["> **KILL SWITCH ACTIVE** - no new BUY calls until a manual review/reset.", ""]
    if not c["strategies_in_use"] and not c.get("shadow"):
        L += ["> **No strategy currently meets the evidence threshold on real NSE data, so there are no BUY calls.** "
              "Existing positions (if any) are still managed below. The monthly reassessment re-checks automatically.",
              ""]
    L += [f"{h}# BUY ({len(c['buy'])})", ""]
    if c["buy"]:
        L += ["| Stock | Weight | Qty for your capital | Last close | Stop-loss | Strategy | Why |", "|---|---|---|---|---|---|---|"]
        for b in c["buy"]:
            L.append(f"| **{b['symbol']}** | {b['weight_pct']}% | {b['qty_for_your_capital']} | {b['last_close']} | "
                     f"{b['stop_loss']} ({b['stop_loss_pct']}%) | {b['strategy']} | {b['reason'].replace('SIGNAL_ENTRY: ', '')} |")
        L.append("")
        L.append("Buy at the next open. Place the stop-loss immediately (stop-loss-market order) and keep it until a SELL call.")
    else:
        L.append("None.")
    L += ["", f"{h}# SELL ({len(c['sell'])})", ""]
    if c["sell"]:
        L += ["| Stock | Entry | Last close | P&L | Why |", "|---|---|---|---|---|"]
        for s in c["sell"]:
            L.append(f"| **{s['symbol']}** | {s['entry_price']} ({s['entry_date']}) | {s['last_close']} | "
                     f"{s['unrealised_pct']}% | {s['reason']} |")
        L.append("")
        L.append("Sell at the next open.")
    else:
        L.append("None.")
    if c["exited_today"]:
        L += ["", f"{h}## Already exited today (stop-loss / write-off)", ""]
        L += [f"- {e['symbol']} ({e['strategy']}): {e['exit_reason']}, {100 * e['return_pct']:.1f}%" for e in c["exited_today"]]
    L += ["", f"{h}# HOLD ({len(c['hold'])})", ""]
    if c["hold"]:
        L += ["| Stock | Entry | Last close | P&L | Stop-loss | Weight | Days |", "|---|---|---|---|---|---|---|"]
        for h in sorted(c["hold"], key=lambda x: -x["unrealised_pct"]):
            L.append(f"| {h['symbol']} | {h['entry_price']} ({h['entry_date']}) | {h['last_close']} | "
                     f"{h['unrealised_pct']}% | {h['stop_loss']} | {h['weight_pct']}% | {h['days_held']} |")
    else:
        L.append("None.")
    t = c["track_record"]
    L += ["", f"{h}# Live track record of these calls (model portfolio, after all costs)", "",
          f"Since {t['model_portfolio_start']} ({t['sessions']} sessions): return **{t['model_return_pct']}%**, "
          f"max drawdown {t['max_drawdown_pct']}%, closed calls {t['closed_calls']}, hit rate "
          f"{t['hit_rate_pct'] if t['hit_rate_pct'] is not None else 'n/a'}%, average per closed call "
          f"{t['avg_return_per_closed_call_pct'] if t['avg_return_per_closed_call_pct'] is not None else 'n/a'}%.", ""]
    if c["strategies_in_use"]:
        L += [f"{h}# Strategies in use (research evidence, re-checked monthly)", "",
              "| Strategy | Status | Score | Walk-forward OOS Sharpe | Test CAGR (net) | Test max DD | Test win rate |",
              "|---|---|---|---|---|---|---|"]
        for k, v in c["strategies_in_use"].items():
            L.append(f"| {k} | {v.get('status')} | {v.get('score')} | {v.get('wf_oos_sharpe') or 0:.2f} | "
                     f"{_pct(v.get('test_cagr_net'))} | {_pct(v.get('test_max_drawdown'))} | {_pct(v.get('test_win_rate'))} |")
        L.append("")
    return "\n".join(L)


def render_daily(validated: dict, shadow: dict | None) -> str:
    """One message per day: validated calls first, then the shadow book (if enabled)."""
    nxt = validated["next_session"]
    L = [f"# Trading calls for {nxt}", "",
         f"NSE close **{validated['session']}** · regime **{validated['regime']}** · generated {validated['generated_ist']}", ""]
    L.append(render_markdown(validated, heading_level=2))
    if shadow is not None:
        L += ["", render_markdown(shadow, heading_level=2)]
    L += ["", "---", f"_{DISCLAIMER}_", "", f"_{PAPER_ONLY_NOTICE}_"]
    return "\n".join(L)


def write_calls(cfg: AppConfig, allowed: dict[str, bool] | None = None,
                shadow_allowed: dict[str, bool] | None = None) -> dict[str, Path]:
    c = build_calls(cfg, allowed)
    sh = None
    if cfg.settings.paper_trading.shadow.enabled and shadow_allowed is not None:
        try:
            sh = build_calls(cfg, shadow_allowed, shadow=True)
        except RuntimeError:
            sh = None          # shadow book not started yet
    out = cfg.path("reports_dir") / "calls"
    out.mkdir(parents=True, exist_ok=True)
    md = render_daily(c, sh)
    if sh is not None:
        c = {**c, "shadow_book": sh}
    stem = f"calls_{c['next_session'].replace('-', '')}"
    (out / f"{stem}.md").write_text(md, encoding="utf-8")
    (out / f"{stem}.json").write_text(json.dumps(c, indent=2, default=str))
    (out / "latest.md").write_text(md, encoding="utf-8")
    (out / "latest.json").write_text(json.dumps(c, indent=2, default=str))
    return {"md": out / f"{stem}.md", "json": out / f"{stem}.json", "latest": out / "latest.md",
            "next_session": c["next_session"], "n_buy": len(c["buy"]), "n_sell": len(c["sell"]),
            "n_hold": len(c["hold"]),
            "shadow": None if sh is None else {"strategies": list(sh["strategies_in_use"]), "n_buy": len(sh["buy"]),
                                               "n_sell": len(sh["sell"]), "n_hold": len(sh["hold"])}}
