"""Daily / weekly / monthly / annual paper-portfolio reports (Markdown, HTML, CSV, Excel, PDF)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .. import PAPER_ONLY_NOTICE
from ..backtesting.metrics import compute_metrics
from ..paper_trading.ledger import PaperLedger
from . import charts
from .exports import to_excel, to_pdf
from .strategy_report import write_html

PERIOD_DAYS = {"daily": 1, "weekly": 7, "monthly": 31, "annual": 366}


def load_portfolio_frames(ledger: PaperLedger, portfolio: str) -> dict[str, pd.DataFrame]:
    q = ledger.query
    eq = q("SELECT * FROM equity WHERE portfolio=? ORDER BY date", (portfolio,))
    if not eq.empty:
        eq["date"] = pd.to_datetime(eq["date"])
    return {
        "equity": eq,
        "trades": q("SELECT * FROM trades WHERE portfolio=? ORDER BY exit_date", (portfolio,)),
        "fills": q("SELECT * FROM fills WHERE portfolio=? ORDER BY date", (portfolio,)),
        "signals": q("SELECT * FROM signals WHERE portfolio=? ORDER BY signal_date", (portfolio,)),
        "orders": q("SELECT * FROM order_events WHERE portfolio=? ORDER BY id", (portfolio,)),
        "risk_events": q("SELECT * FROM risk_events WHERE portfolio=? ORDER BY date", (portfolio,)),
        "positions": q("SELECT * FROM positions_eod WHERE portfolio=? AND date=(SELECT MAX(date) FROM positions_eod "
                       "WHERE portfolio=?)", (portfolio, portfolio)),
        "audit": q("SELECT * FROM audit_log WHERE portfolio=? ORDER BY id DESC LIMIT 200", (portfolio,)),
    }


def build_period_report(ledger: PaperLedger, portfolio: str, period: str, out_root: Path,
                        benchmark: pd.Series | None = None, as_of=None, synthetic: bool = False,
                        rf: float = 0.065, extra_sections: list[tuple[str, object]] | None = None) -> dict[str, Path]:
    fr = load_portfolio_frames(ledger, portfolio)
    eq = fr["equity"]
    if eq.empty:
        raise RuntimeError("no paper-trading history to report on")
    end = pd.Timestamp(as_of) if as_of else eq["date"].max()
    start = end - pd.Timedelta(days=PERIOD_DAYS[period] - 1)
    win = eq[(eq["date"] >= start) & (eq["date"] <= end)]
    prev = eq[eq["date"] < start].tail(1)
    base = pd.concat([prev, win]) if len(prev) else win
    period_ret = float(base["equity"].iloc[-1] / base["equity"].iloc[0] - 1) if len(base) > 1 else 0.0
    trades = fr["trades"]
    tw = trades[(pd.to_datetime(trades["exit_date"]) >= start) & (pd.to_datetime(trades["exit_date"]) <= end)] \
        if not trades.empty else trades
    sig = fr["signals"]
    sw = sig[(pd.to_datetime(sig["signal_date"]) >= start) & (pd.to_datetime(sig["signal_date"]) <= end)] \
        if not sig.empty else sig
    rk = fr["risk_events"]
    rw = rk[(pd.to_datetime(rk["date"]) >= start) & (pd.to_datetime(rk["date"]) <= end)] if not rk.empty else rk
    life = compute_metrics(eq, trades, benchmark, rf)
    last = eq.iloc[-1]
    pos = fr["positions"]
    snapshot = {
        "as_of": str(end.date()), "period": period, "period_start": str(start.date()),
        "simulated_capital_initial": float(eq["equity"].iloc[0]) if len(eq) else np.nan,
        "equity_net": float(last["equity"]), "cash": float(last["cash"]), "invested": float(last["invested"]),
        "exposure": float(last["exposure"]), "open_positions": int(last["n_positions"]),
        "unrealised_pnl": float(pos["unrealised_pnl"].sum()) if not pos.empty else 0.0,
        "realised_pnl_period": float(tw["net_pnl"].sum()) if not tw.empty else 0.0,
        "realised_pnl_lifetime": float(trades["net_pnl"].sum()) if not trades.empty else 0.0,
        "period_return_net": period_ret,
        "drawdown_from_peak": float(last["equity"] / eq["equity"].cummax().iloc[-1] - 1),
        "charges_lifetime": float(last["cum_charges"]), "exec_cost_lifetime": float(last["cum_exec_cost"]),
        "kill_switch": bool(last["kill_switch"]), "regime": last["regime"],
    }
    tag = f"{period}_{end:%Y%m%d}"
    out = out_root / "paper" / portfolio / period
    out.mkdir(parents=True, exist_ok=True)
    wm = "SYNTHETIC DATA - SOFTWARE TEST ONLY" if synthetic else None
    md = [f"# {period.title()} paper-trading report - {portfolio}", "",
          (f"> **{wm}**\n\n" if wm else "") + f"> {PAPER_ONLY_NOTICE}", "",
          "## Portfolio snapshot", "", "| Item | Value |", "|---|---|",
          *[f"| {k} | {v:,.4f} |" if isinstance(v, float) else f"| {k} | {v} |" for k, v in snapshot.items()], "",
          "## Lifetime performance (net of simulated costs)", "", "| Metric | Value |", "|---|---|",
          *[f"| {k} | {v:.4f} |" if isinstance(v, float) else f"| {k} | {v} |"
            for k, v in life.items() if k in ("cagr_net", "cagr_gross", "sharpe_net", "sortino_net", "max_drawdown",
                                              "volatility_ann", "cvar_95_daily", "trades", "win_rate", "profit_factor",
                                              "benchmark_cagr", "total_charges_inr", "total_exec_cost_inr")], "",
          f"## Open positions ({len(pos)})", "", _simple_table(pos.drop(columns=["portfolio"], errors="ignore")), "",
          f"## Closed trades this period ({len(tw)})", "", _simple_table(tw[["symbol", "strategy", "entry_date", "exit_date",
                                                                           "net_pnl", "return_pct", "exit_reason"]]
                                                                       if not tw.empty else tw), "",
          f"## Signals this period ({len(sw)})", "",
          _simple_table(sw.groupby(["strategy", "decision"]).size().rename("n").reset_index() if not sw.empty else sw), "",
          f"## Risk events this period ({len(rw)})", "", _simple_table(rw[["date", "severity", "code", "detail"]]
                                                                       if not rw.empty else rw)]
    for h, c in extra_sections or []:
        md += ["", f"## {h}", "", c if isinstance(c, str) else _simple_table(c)]
    md_text = "\n".join(md)
    (out / f"report_{tag}.md").write_text(md_text, encoding="utf-8")
    figs = [charts.equity_curve(eq, benchmark, "Paper portfolio equity (net vs gross)"),
            charts.drawdown_chart(eq.set_index("date")["equity"], "Paper portfolio drawdown"),
            charts.sector_allocation(pos)]
    if len(eq) > 40:
        figs.append(charts.monthly_heatmap(eq.set_index("date")["equity"]))
    if not trades.empty:
        figs.append(charts.trade_distribution(trades))
    html = write_html(md_text, figs, out / f"report_{tag}.html", f"{period} report {portfolio}")
    csv_dir = out / f"csv_{tag}"
    csv_dir.mkdir(exist_ok=True)
    for k in ("equity", "trades", "fills", "signals", "orders", "risk_events", "positions"):
        fr[k].to_csv(csv_dir / f"{k}.csv", index=False)
    xlsx = to_excel({"snapshot": pd.DataFrame([snapshot]), "equity": eq, "positions": pos, "trades": trades,
                     "signals_period": sw, "risk_events": rk, "fills": fr["fills"]}, out / f"report_{tag}.xlsx")
    pdf = to_pdf(f"{period.title()} paper-trading report - {portfolio}",
                 [("Snapshot", snapshot), ("Lifetime metrics", {k: v for k, v in life.items()
                                                                if not isinstance(v, (dict, list))}),
                  ("Open positions", pos.drop(columns=["portfolio"], errors="ignore")),
                  ("Closed trades this period", tw.drop(columns=["portfolio", "entry_info_json"], errors="ignore")),
                  ("Risk events this period", rw)], out / f"report_{tag}.pdf", watermark=wm)
    (out / f"snapshot_{tag}.json").write_text(json.dumps(snapshot, indent=2, default=str))
    return {"md": out / f"report_{tag}.md", "html": html, "xlsx": xlsx, "pdf": pdf, "csv_dir": csv_dir}


def _simple_table(df: pd.DataFrame, max_rows: int = 50) -> str:
    if df is None or df.empty:
        return "_None._"
    d = df.head(max_rows)
    cols = list(d.columns)
    lines = ["| " + " | ".join(map(str, cols)) + " |", "|" + "---|" * len(cols)]
    for row in d.itertuples(index=False):
        cells = []
        for v in row:
            if isinstance(v, float):
                cells.append(f"{v:,.4f}" if abs(v) < 1000 else f"{v:,.0f}")
            else:
                cells.append(str(v).replace("|", "/"))
        lines.append("| " + " | ".join(cells) + " |")
    if len(df) > max_rows:
        lines.append(f"\n_{len(df) - max_rows} more rows in CSV export._")
    return "\n".join(lines)
