"""Bharat Quant Lab - Streamlit dashboard (read-mostly; research & paper trading only)."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bql import PAPER_ONLY_NOTICE  # noqa: E402
from bql.backtesting.metrics import compute_metrics, drawdown_series  # noqa: E402
from bql.config import load_config  # noqa: E402
from bql.data import MarketStore  # noqa: E402
from bql.reporting import charts  # noqa: E402

WS = Path(os.environ.get("BQL_ROOT", ROOT)).resolve()
st.set_page_config(page_title="Bharat Quant Lab", layout="wide", page_icon="📈")


@st.cache_resource
def get_cfg():
    return load_config(WS / "config", WS)


cfg = get_cfg()
PORTFOLIO = cfg.settings.paper_trading.portfolio_name
REPORTS = cfg.path("reports_dir")


@st.cache_data(ttl=120)
def market_data():
    p = cfg.path("market_db")
    if not p.exists():
        return None
    store = MarketStore(p, read_only=True)
    last = store.last_price_date()
    # the overview pages need ~1 year of stock data; full history stays in DuckDB
    prices = store.prices(start=(last - pd.Timedelta(days=420)) if last is not None else None)
    bench_all = store.table("benchmarks")
    sec = store.table("securities")
    prov = store.table("provenance")
    return prices, bench_all, sec, prov, store.any_synthetic()


@st.cache_data(ttl=60)
def ledger(sql: str, params: tuple = ()):
    p = cfg.path("paper_db")
    if not p.exists():
        return pd.DataFrame()
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        return pd.read_sql_query(sql, con, params=params)
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
    finally:
        con.close()


def latest_research():
    lp = REPORTS / "research" / "latest.json"
    if not lp.exists():
        return None, None
    meta = json.loads(lp.read_text())
    return meta, REPORTS / "research" / meta["run_id"]


md = market_data()
synthetic = bool(md and md[4])

# ------------------------------------------------------------------ header
st.title("Bharat Quant Lab")
st.caption(PAPER_ONLY_NOTICE)
if synthetic:
    st.error("SYNTHETIC DATA LOADED - software-test workspace. Nothing shown here is evidence of real-market "
             "performance.", icon="⚠️")
status_file = cfg.path("state_dir") / "last_run_status.json"
if status_file.exists():
    s = json.loads(status_file.read_text())
    msg = f"Last workflow: **{s['workflow']}** `{s['key']}` → **{s['status']}** at {s['finished_ist']}"
    (st.success if s["status"].startswith("COMPLETED") or s["status"].startswith("SKIPPED") else st.warning)(msg)
    if s.get("error"):
        st.code(s["error"][:2000])

section = st.sidebar.radio("Section", ["Market overview", "Strategy research", "Portfolio", "Risk management",
                                       "Backtesting", "Trade journal", "Reports"])
st.sidebar.markdown(f"Workspace: `{WS}`\n\nConfig hash: `{cfg.config_hash()}`")

if md is None:
    st.info("No market database yet. Run `python main.py init` and import data (see docs/DATA_IMPORT_GUIDE.md).")
    st.stop()
prices, bench_all, sec, prov, _ = md
bname = cfg.settings.backtest.benchmark

# ================================================================== 1. market overview
if section == "Market overview":
    from bql.regime import detect_from_cfg

    b = bench_all[bench_all["index_name"] == bname].sort_values("date").set_index("date")["close"]
    if b.empty:
        st.warning(f"Benchmark {bname} not loaded.")
        st.stop()
    reg = detect_from_cfg(b, cfg.strategies.regime)
    last = reg.iloc[-1]
    c = st.columns(5)
    c[0].metric(bname, f"{b.iloc[-1]:,.2f}", f"{b.iloc[-1] / b.iloc[-2] - 1:+.2%}")
    c[1].metric("Market regime", last["regime"])
    c[2].metric("Realised vol (21d, ann.)", f"{last['vol_ann']:.1%}", f"pct rank {last['vol_pct']:.0%}")
    c[3].metric("Drawdown from 1y high", f"{last['drawdown']:.1%}")
    c[4].metric("Low-volatility flag", "Yes" if last["low_volatility"] else "No")
    others = sorted(set(bench_all["index_name"]) - {bname})
    st.caption("Other indices loaded: " + (", ".join(others) if others else "none (import e.g. NIFTY500 to display)"))
    yrs = st.slider("Years shown", 1, 20, 5)
    bb = b[b.index >= b.index.max() - pd.DateOffset(years=yrs)]
    import plotly.graph_objects as go

    fig = go.Figure(go.Scatter(x=bb.index, y=bb, name=bname, line=dict(color=charts.NET)))
    rr = reg.loc[bb.index, "regime"]
    colors = {"MARKET_STRESS": "rgba(178,59,59,0.25)", "STRONG_BEARISH": "rgba(178,59,59,0.12)",
              "HIGH_VOLATILITY": "rgba(194,113,27,0.15)"}
    for name, col in colors.items():
        m = rr == name
        if m.any():
            fig.add_trace(go.Bar(x=bb.index[m], y=[bb.max()] * int(m.sum()), marker_color=col, name=name,
                                 hoverinfo="skip", width=86400000))
    fig.update_layout(barmode="overlay", height=380, template="plotly_white", title=f"{bname} with stress regimes")
    st.plotly_chart(fig, width="stretch")
    st.subheader("Regime distribution (sessions)")
    st.bar_chart(reg["regime"].value_counts())
    # breadth & sectors from stock data
    px = prices.pivot_table(index="date", columns="symbol", values="close").sort_index()
    ret = px.pct_change(fill_method=None)
    adv = (ret > 0).sum(axis=1)
    dec = (ret < 0).sum(axis=1)
    c = st.columns(3)
    c[0].metric("Advances (last session)", int(adv.iloc[-1]))
    c[1].metric("Declines (last session)", int(dec.iloc[-1]))
    c[2].metric("A/D ratio", f"{adv.iloc[-1] / max(dec.iloc[-1], 1):.2f}")
    st.line_chart((adv - dec).cumsum().tail(252 * yrs).rename("Cumulative advance-decline line"))
    secmap = dict(zip(sec["symbol"], sec["sector"]))
    perf = pd.DataFrame({"1M": px.iloc[-1] / px.iloc[-22] - 1, "3M": px.iloc[-1] / px.iloc[-64] - 1,
                         "12M": px.iloc[-1] / px.iloc[-253] - 1})
    perf["sector"] = perf.index.map(secmap).fillna("UNKNOWN")
    st.subheader("Sector performance (median constituent return, unadjusted prices)")
    st.caption("Uses raw closes: a corporate action inside the window can distort a constituent - median limits this. "
               "Sectors are NSE industry classifications from current index lists.")
    st.dataframe(perf.groupby("sector")[["1M", "3M", "12M"]].median().style.format("{:.1%}"), width="stretch")
    xc = WS / cfg.settings.exchange_data.build_dir / "nse_bse_crosscheck.json"
    if xc.exists():
        st.subheader("Independent NSE vs BSE close cross-check (same ISIN, same date)")
        st.json(json.loads(xc.read_text()))

# ================================================================== 2. strategy research
elif section == "Strategy research":
    from bql.strategies import STRATEGIES

    meta, run_dir = latest_research()
    if meta is None:
        st.info("No research run yet: `python main.py research`.")
        st.stop()
    st.subheader(f"Latest research run `{meta['run_id']}` ({meta['created_ist']})")
    flags = meta.get("data_flags", {})
    st.caption(f"Data flags: {flags}. Statuses are capped when data are synthetic or not point-in-time.")
    summ = pd.read_csv(run_dir / "summary.csv")
    st.dataframe(summ, width="stretch", hide_index=True)
    st.plotly_chart(charts.strategy_comparison(summ), width="stretch")
    pick = st.selectbox("Strategy", list(summ["strategy"]))
    ev = json.loads((run_dir / pick / "evaluation.json").read_text())
    st.markdown(f"### {pick} - **{ev['status']}** (score {ev['score']['total']}/100)")
    st.markdown(f"**Hypothesis.** {ev['describe']['hypothesis']}\n\n**Logic.** {ev['describe']['description']}")
    st.code(ev["describe"]["mathematics"])
    st.write("Status reasons:", ev["status_reasons"] or "all gates passed")
    cols = ["cagr_net", "cagr_gross", "sharpe_net", "sortino_net", "max_drawdown", "trades", "win_rate",
            "profit_factor", "total_charges_inr", "total_exec_cost_inr"]
    tbl = pd.DataFrame({"Training (IN-SAMPLE)": ev["train"], "Validation": ev["validation"],
                        "Final test (OOS)": ev["test"]}).reindex(cols)
    st.dataframe(tbl, width="stretch")
    curves = pd.read_parquet(run_dir / pick / "curves.parquet")
    st.plotly_chart(charts.returns_curve({c: curves[c].dropna() for c in curves.columns},
                                         "Growth of 1 by segment (in-sample labelled)"), width="stretch")
    c1, c2 = st.columns(2)
    c1.markdown("**Walk-forward folds**")
    c1.dataframe(pd.DataFrame(ev["walk_forward"]["folds"]), hide_index=True)
    c2.markdown("**Robustness scenarios**")
    c2.dataframe(pd.DataFrame({k: v for k, v in ev["robustness"].items() if isinstance(v, dict) and "sharpe" in v}).T)
    st.markdown("**Statistics**")
    st.json(ev["statistics"], expanded=False)
    st.markdown("**Weaknesses**")
    for w in ev["weaknesses"]:
        st.markdown(f"- {w}")
    with st.expander("All implemented strategies"):
        for name, cls in STRATEGIES.items():
            st.markdown(f"**{name}** ({cls.family}) - {cls.description}")

# ================================================================== 3. portfolio
elif section == "Portfolio":
    eq = ledger("SELECT * FROM equity WHERE portfolio=? ORDER BY date", (PORTFOLIO,))
    if eq.empty:
        st.info("No paper-trading history yet: `python main.py paper run` or the daily workflow.")
        st.stop()
    eq["date"] = pd.to_datetime(eq["date"])
    pos = ledger("SELECT * FROM positions_eod WHERE portfolio=? AND date=(SELECT MAX(date) FROM positions_eod "
                 "WHERE portfolio=?)", (PORTFOLIO, PORTFOLIO))
    trades = ledger("SELECT * FROM trades WHERE portfolio=?", (PORTFOLIO,))
    pf = ledger("SELECT * FROM portfolios WHERE name=?", (PORTFOLIO,))
    last = eq.iloc[-1]
    init = float(pf["initial_capital"].iloc[0]) if not pf.empty else float(eq["equity"].iloc[0])
    c = st.columns(6)
    c[0].metric("Equity (net)", f"₹{last['equity']:,.0f}", f"{last['equity'] / init - 1:+.2%} since start")
    c[1].metric("Cash", f"₹{last['cash']:,.0f}")
    c[2].metric("Invested", f"₹{last['invested']:,.0f}", f"{last['exposure']:.0%} exposure")
    c[3].metric("Unrealised P&L", f"₹{pos['unrealised_pnl'].sum() if not pos.empty else 0:,.0f}")
    c[4].metric("Realised P&L (net)", f"₹{trades['net_pnl'].sum() if not trades.empty else 0:,.0f}")
    dd = drawdown_series(eq.set_index("date")["equity"])
    c[5].metric("Drawdown", f"{dd.iloc[-1]:.2%}", f"max {dd.min():.2%}")
    if not pf.empty and int(pf["test_mode"].iloc[0]):
        st.warning("TEST MODE portfolio: unvalidated strategies were allowed for software testing.")
    b = bench_all[bench_all["index_name"] == bname].set_index("date")["close"]
    st.plotly_chart(charts.equity_curve(eq, b, "Paper portfolio - net of simulated costs (gross dotted)"),
                    width="stretch")
    st.plotly_chart(charts.drawdown_chart(eq.set_index("date")["equity"]), width="stretch")
    st.subheader(f"Open positions ({len(pos)})")
    st.dataframe(pos.drop(columns=["portfolio"], errors="ignore"), width="stretch", hide_index=True)
    st.plotly_chart(charts.sector_allocation(pos), width="stretch")
    m = compute_metrics(eq, trades, b, cfg.settings.backtest.risk_free_rate_annual)
    st.subheader("Performance (net primary)")
    st.dataframe(pd.Series(m, name="value").astype(str), width="stretch")

# ================================================================== 4. risk
elif section == "Risk management":
    eq = ledger("SELECT * FROM equity WHERE portfolio=? ORDER BY date", (PORTFOLIO,))
    snap = ledger("SELECT state_json FROM state_snapshots WHERE portfolio=? ORDER BY session_date DESC LIMIT 1",
                  (PORTFOLIO,))
    events = ledger("SELECT * FROM risk_events WHERE portfolio=? ORDER BY date DESC", (PORTFOLIO,))
    pos = ledger("SELECT * FROM positions_eod WHERE portfolio=? AND date=(SELECT MAX(date) FROM positions_eod "
                 "WHERE portfolio=?)", (PORTFOLIO, PORTFOLIO))
    L = cfg.risk
    if snap.empty:
        st.info("No paper portfolio state yet.")
        st.stop()
    risk = json.loads(snap["state_json"].iloc[0])["risk"]
    c = st.columns(4)
    c[0].metric("Kill switch", "ACTIVE" if risk["kill_switch"] else "off")
    c[1].metric("Suspended strategies", len(risk["suspended"]))
    c[2].metric("Entry block until", risk.get("entry_block_until") or "-")
    last = eq.iloc[-1]
    dd = last["equity"] / eq["equity"].cummax().iloc[-1] - 1
    c[3].metric("Drawdown vs kill limit", f"{dd:.2%}", f"limit {-L.loss_limits.max_drawdown_kill:.0%}")
    if risk["kill_switch"]:
        st.error(f"KILL SWITCH since {risk['kill_date']}: {risk['kill_reason']}. New simulated entries are blocked. "
                 "Manual reset required: `python main.py paper reset-kill-switch --operator NAME --note TEXT`.")
    elif dd <= -L.loss_limits.max_drawdown_warning:
        st.warning(f"Drawdown {dd:.2%} beyond warning level {-L.loss_limits.max_drawdown_warning:.0%}.")
    if risk["suspended"]:
        st.warning(f"Suspended: {risk['suspended']}")
    st.subheader("Exposure & concentration")
    c = st.columns(3)
    c[0].metric("Gross exposure", f"{last['exposure']:.0%}", f"cap {L.portfolio.max_gross_exposure:.0%}")
    c[1].metric("Open positions", int(last["n_positions"]), f"max {L.position.max_open_positions}")
    if not pos.empty:
        sw = pos.groupby("sector")["market_value"].sum() / last["equity"]
        c[2].metric("Largest sector", f"{sw.idxmax()} {sw.max():.0%}", f"cap {L.portfolio.max_sector_exposure:.0%}")
        st.bar_chart(sw.rename("weight"))
        pxw = prices[prices["symbol"].isin(pos["symbol"])].pivot_table(index="date", columns="symbol", values="close")
        corr = pxw.pct_change(fill_method=None).tail(L.portfolio.correlation_lookback_days).corr()
        if corr.shape[0] > 1:
            st.plotly_chart(charts.correlation_heatmap(corr, "Holdings correlation (120d)"), width="stretch")
    st.subheader("Risk-limit configuration")
    st.json(L.model_dump(), expanded=False)
    st.subheader("Risk events")
    st.dataframe(events, width="stretch", hide_index=True)

# ================================================================== 5. backtesting
elif section == "Backtesting":
    from bql.pipeline import run_single_backtest
    from bql.strategies import STRATEGIES

    st.warning("A single full-period backtest is IN-SAMPLE evidence only. Use the research engine for validation.")
    name = st.selectbox("Strategy", list(STRATEGIES))
    params = st.text_area("Parameters (JSON)", json.dumps(cfg.strategies.strategies[name].params))
    c1, c2 = st.columns(2)
    start = c1.text_input("Start (YYYY-MM-DD, optional)")
    end = c2.text_input("End (optional)")
    if st.button("Run backtest"):
        with st.spinner("Running data-quality gate and simulation..."):
            res = run_single_backtest(cfg, name, json.loads(params), start or None, end or None)
        out = res["out_dir"]
        eqb = pd.read_csv(out / "equity.csv", parse_dates=["date"])
        trb = pd.read_csv(out / "trades.csv") if (out / "trades.csv").stat().st_size > 2 else pd.DataFrame()
        m = res["metrics"]
        c = st.columns(5)
        c[0].metric("CAGR (net)", f"{m['cagr_net']:.2%}", f"gross {m['cagr_gross']:.2%}")
        c[1].metric("Sharpe (net)", f"{m['sharpe_net']:.2f}")
        c[2].metric("Max drawdown", f"{m['max_drawdown']:.2%}")
        c[3].metric("Trades", m["trades"])
        c[4].metric("Costs (INR)", f"{m['total_charges_inr'] + m['total_exec_cost_inr']:,.0f}")
        b = bench_all[bench_all["index_name"] == bname].set_index("date")["close"]
        st.plotly_chart(charts.equity_curve(eqb, b), width="stretch")
        e = eqb.set_index("date")["equity"]
        st.plotly_chart(charts.drawdown_chart(e), width="stretch")
        st.plotly_chart(charts.monthly_heatmap(e), width="stretch")
        st.plotly_chart(charts.yearly_bars(e, b), width="stretch")
        st.plotly_chart(charts.rolling_stats(e.pct_change().dropna()), width="stretch")
        st.plotly_chart(charts.trade_distribution(trb), width="stretch")
        st.subheader("Transaction-cost analysis")
        st.write({"statutory + brokerage (INR)": m["total_charges_inr"],
                  "spread/slippage/impact (INR)": m["total_exec_cost_inr"],
                  "annual cost drag (CAGR gross - net)": m.get("cost_drag_annual"),
                  "annual turnover (x equity)": m.get("annual_turnover")})
        st.json(m, expanded=False)

# ================================================================== 6. journal
elif section == "Trade journal":
    trades = ledger("SELECT * FROM trades WHERE portfolio=? ORDER BY exit_date DESC", (PORTFOLIO,))
    sigs = ledger("SELECT * FROM signals WHERE portfolio=? ORDER BY signal_date DESC LIMIT 5000", (PORTFOLIO,))
    fills = ledger("SELECT * FROM fills WHERE portfolio=? ORDER BY date DESC", (PORTFOLIO,))
    if trades.empty and sigs.empty:
        st.info("No paper-trading records yet.")
        st.stop()
    strat = st.multiselect("Strategies", sorted(set(trades.get("strategy", pd.Series()).dropna()) |
                                                set(sigs.get("strategy", pd.Series()).dropna())))
    outcome = st.radio("Outcome", ["All", "Winners", "Losers"], horizontal=True)
    t = trades.copy()
    if strat:
        t = t[t["strategy"].isin(strat)]
        sigs = sigs[sigs["strategy"].isin(strat)]
    if outcome == "Winners":
        t = t[t["net_pnl"] > 0]
    elif outcome == "Losers":
        t = t[t["net_pnl"] <= 0]
    st.subheader("Strategy-wise results")
    if not trades.empty:
        st.dataframe(trades.groupby("strategy").agg(trades=("net_pnl", "size"), net_pnl=("net_pnl", "sum"),
                                                    win_rate=("net_pnl", lambda s: (s > 0).mean()),
                                                    avg_return=("return_pct", "mean"),
                                                    avg_hold=("holding_days", "mean")), width="stretch")
    st.subheader(f"Closed trades ({len(t)})")
    st.dataframe(t.drop(columns=["portfolio"], errors="ignore"), width="stretch", hide_index=True)
    st.subheader("Fills")
    st.dataframe(fills.drop(columns=["portfolio", "charges_json"], errors="ignore"), width="stretch", hide_index=True)
    st.subheader("Signals & decisions (with reasons and indicator values)")
    st.dataframe(sigs.drop(columns=["portfolio"], errors="ignore"), width="stretch", hide_index=True)

# ================================================================== 7. reports
elif section == "Reports":
    from bql.paper_trading import PaperLedger
    from bql.reporting import build_period_report

    c = st.columns(4)
    for i, period in enumerate(["daily", "weekly", "monthly", "annual"]):
        if c[i].button(f"Generate {period} report"):
            b = bench_all[bench_all["index_name"] == bname].set_index("date")["close"]
            try:
                paths = build_period_report(PaperLedger(cfg.path("paper_db")), PORTFOLIO, period, REPORTS, b,
                                            None, synthetic, cfg.settings.backtest.risk_free_rate_annual)
                st.success(f"Written: {paths['html'].name}")
            except RuntimeError as e:
                st.error(str(e))
    files = sorted([p for p in REPORTS.rglob("*") if p.suffix in (".html", ".pdf", ".xlsx", ".md", ".csv")
                    and "csv_" not in str(p.parent.name)], key=lambda p: p.stat().st_mtime, reverse=True)[:200]
    kind = st.radio("Type", ["pdf", "xlsx", "html", "md", "csv"], horizontal=True)
    for p in [f for f in files if f.suffix == f".{kind}"][:60]:
        with open(p, "rb") as fh:
            st.download_button(f"{p.relative_to(REPORTS)}", fh.read(), file_name=p.name, key=str(p))
    st.subheader("Data provenance")
    st.dataframe(prov, width="stretch", hide_index=True)
