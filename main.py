#!/usr/bin/env python
"""Bharat Quant Lab command-line interface.

RESEARCH / PAPER TRADING ONLY - there is no broker connectivity and no live order placement.

Examples
--------
  python main.py init
  python main.py import --dataset prices --file data/raw/nse_eod_2015_2025.csv
  python main.py fetch-exchange   # official NSE/BSE archives -> database (resumable)
  python main.py validate-data
  python main.py backtest --strategy trend_ma
  python main.py research
  python main.py paper run
  python main.py paper status
  python main.py paper reset-kill-switch --operator "A. Analyst" --note "reviewed drawdown, limits unchanged"
  python main.py report --period weekly
  python main.py schedule daily
  python main.py demo            # end-to-end run on SYNTHETIC data in ./demo_workspace
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from bql import PAPER_ONLY_NOTICE  # noqa: E402
from bql.config import load_config  # noqa: E402
from bql.logging_setup import setup_logging  # noqa: E402


def _cfg(args):
    root = Path(args.root).resolve() if args.root else ROOT
    cdir = Path(args.config).resolve() if args.config else (root / "config")
    cfg = load_config(cdir, root)
    setup_logging(cfg.path("logs_dir"))
    return cfg


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_init(args):
    from bql.data import MarketStore
    from bql.paper_trading import PaperLedger
    from bql.pipeline import ensure_dirs

    cfg = _cfg(args)
    ensure_dirs(cfg)
    MarketStore(cfg.path("market_db"))
    PaperLedger(cfg.path("paper_db"))
    _print({"status": "initialised", "market_db": cfg.path("market_db"), "paper_db": cfg.path("paper_db"),
            "config_hash": cfg.config_hash(), "notice": PAPER_ONLY_NOTICE})


def cmd_import(args):
    from bql.pipeline import ensure_dirs, import_file, import_inbox

    cfg = _cfg(args)
    ensure_dirs(cfg)
    if args.inbox:
        _print(import_inbox(cfg))
    else:
        _print(import_file(cfg, args.dataset, Path(args.file), args.license_note))


def cmd_fetch_exchange(args):
    """Download official NSE/BSE archive files and import them (resumable; rate-limited)."""
    from bql.pipeline import ensure_dirs, fetch_exchange_data

    cfg = _cfg(args)
    ensure_dirs(cfg)
    _print(fetch_exchange_data(cfg, args.start, args.end, download=not args.no_download))


def cmd_validate(args):
    from bql.pipeline import data_quality

    cfg = _cfg(args)
    rep, paths = data_quality(cfg, "manual")
    _print({"passed": rep.passed, "summary": rep.summary, "excluded_symbols": rep.excluded_symbols, "report": paths})
    return 0 if rep.passed else 2


def cmd_backtest(args):
    from bql.pipeline import run_single_backtest

    cfg = _cfg(args)
    params = json.loads(args.params) if args.params else None
    res = run_single_backtest(cfg, args.strategy, params, args.start, args.end)
    m = res["metrics"]
    keys = ["start", "end", "final_capital_net", "cagr_net", "cagr_gross", "sharpe_net", "max_drawdown", "trades",
            "win_rate", "profit_factor", "total_charges_inr", "total_exec_cost_inr", "benchmark_cagr"]
    _print({"out_dir": res["out_dir"], "WARNING": "single full-period backtest is NOT validated evidence; use `research`",
            **{k: m.get(k) for k in keys}})


def cmd_research(args):
    from bql.pipeline import run_research

    cfg = _cfg(args)
    names = args.strategies.split(",") if args.strategies else None
    res = run_research(cfg, names)
    _print({k: v for k, v in res.items() if k != "portfolio"})


def cmd_paper(args):
    from bql.paper_trading import PaperTrader
    from bql.reporting.periodic import load_portfolio_frames

    cfg = _cfg(args)
    pt = PaperTrader(cfg)
    if args.action == "run":
        _print(pt.run(as_of=args.as_of, start=args.start))
    elif args.action == "status":
        fr = load_portfolio_frames(pt.ledger, pt.name)
        eq = fr["equity"]
        _, state = pt.ledger.load_snapshot(pt.name)
        _print({"portfolio": pt.name, "last_session": pt.ledger.last_session(pt.name),
                "equity": None if eq.empty else eq.iloc[-1].to_dict(),
                "open_positions": fr["positions"].drop(columns=["portfolio"], errors="ignore").to_dict("records"),
                "kill_switch": state["risk"]["kill_switch"] if state else None,
                "kill_reason": state["risk"]["kill_reason"] if state else None,
                "suspended_strategies": state["risk"]["suspended"] if state else None})
    elif args.action == "reset-kill-switch":
        _print(pt.reset_kill_switch(args.operator, args.note))
    elif args.action == "reset-strategy":
        _print(pt.reset_strategy(args.strategy, args.operator, args.note))


def cmd_report(args):
    from bql.data import MarketStore
    from bql.paper_trading import PaperLedger
    from bql.reporting import build_period_report

    cfg = _cfg(args)
    store = MarketStore(cfg.path("market_db"), read_only=True)
    b = store.benchmark(cfg.settings.backtest.benchmark).set_index("date")["close"]
    paths = build_period_report(PaperLedger(cfg.path("paper_db")), cfg.settings.paper_trading.portfolio_name,
                                args.period, cfg.path("reports_dir"), b, args.as_of, store.any_synthetic(),
                                cfg.settings.backtest.risk_free_rate_annual)
    _print(paths)


def cmd_schedule(args):
    from bql.scheduler import daily_workflow, monthly_workflow, weekly_workflow
    from bql.scheduler.workflows import daemon

    cfg = _cfg(args)
    fn = {"daily": daily_workflow, "weekly": weekly_workflow, "monthly": monthly_workflow}
    if args.workflow == "daemon":
        daemon(cfg)
        return 0
    res = fn[args.workflow](cfg, as_of=args.as_of, force=args.force)
    _print(res)
    return 0 if res["status"] in ("COMPLETED", "COMPLETED_NO_SESSION", "SKIPPED_ALREADY_COMPLETED", "STOPPED") else 1


def cmd_generate_synthetic(args):
    from bql.data.synthetic import generate

    _print(generate(Path(args.out), n_symbols=args.symbols, seed=args.seed))


def cmd_demo(args):
    """End-to-end exercise of every component on SYNTHETIC data in an isolated workspace."""
    from bql.data.synthetic import generate
    from bql.paper_trading import PaperTrader
    from bql.pipeline import ensure_dirs, import_file, run_research
    from bql.scheduler import daily_workflow, monthly_workflow, weekly_workflow

    ws = Path(args.workspace).resolve()
    if ws.exists() and args.fresh:
        shutil.rmtree(ws)
    (ws / "config").mkdir(parents=True, exist_ok=True)
    for f in (ROOT / "config").glob("*.yaml"):
        shutil.copy(f, ws / "config" / f.name)
    s = yaml.safe_load((ws / "config" / "settings.yaml").read_text())
    s["backtest"]["benchmark"] = "SYNTH_INDEX"
    s["data"]["default_benchmark"] = "SYNTH_INDEX"
    s["data"]["universe_index"] = "SYNTH30"
    s["data"]["universe_mode"] = "index_membership"
    s["data"]["gaps_are_non_trading"] = False
    s["data"]["critical_row_policy"] = "exclude_symbol"
    s["exchange_data"]["enabled"] = False                       # demo never touches the network
    s["paper_trading"]["portfolio_name"] = "demo_paper"
    s["paper_trading"]["test_mode_allow_unvalidated"] = True   # SOFTWARE TEST ONLY - tagged in audit trail
    (ws / "config" / "settings.yaml").write_text("# DEMO WORKSPACE - SYNTHETIC DATA - SOFTWARE TEST ONLY\n"
                                                 + yaml.safe_dump(s, sort_keys=False))
    st = yaml.safe_load((ws / "config" / "strategies.yaml").read_text())
    st["regime"]["benchmark"] = "SYNTH_INDEX"
    (ws / "config" / "strategies.yaml").write_text(yaml.safe_dump(st, sort_keys=False))
    args.root, args.config = str(ws), str(ws / "config")
    cfg = _cfg(args)
    ensure_dirs(cfg)

    print("[1/7] generating SYNTHETIC data (software test only) ...")
    paths = generate(ws / "data" / "synthetic", seed=args.seed)
    prices = pd.read_csv(paths["prices"], parse_dates=["date"])
    bench = pd.read_csv(paths["benchmarks"], parse_dates=["date"])
    dates = sorted(bench["date"].unique())
    holdout = dates[-args.daily_sessions:]
    cutoff = dates[-args.daily_sessions - 1]
    hist_p = ws / "data" / "synthetic" / "synthetic_prices_history.csv"
    hist_b = ws / "data" / "synthetic" / "synthetic_benchmark_history.csv"
    prices[prices["date"] <= cutoff].to_csv(hist_p, index=False, date_format="%Y-%m-%d")
    bench[bench["date"] <= cutoff].to_csv(hist_b, index=False, date_format="%Y-%m-%d")

    print("[2/7] importing history ...")
    for ds in ("securities", "corporate_actions", "index_membership", "holidays", "price_bands"):
        import_file(cfg, ds, paths[ds], "SYNTHETIC test data")
    import_file(cfg, "benchmarks", hist_b, "SYNTHETIC test data")
    import_file(cfg, "prices", hist_p, "SYNTHETIC test data")

    print("[3/7] research: train/validation/test, walk-forward, robustness, statistics (several minutes) ...")
    res = run_research(cfg)
    _print({"research_statuses": res["statuses"], "scores": res["scores"]})

    print("[4/7] paper-trading backfill (one year before cutoff, TEST MODE) ...")
    start = dates[-args.daily_sessions - 1 - 250]
    out = PaperTrader(cfg).run(as_of=cutoff, start=start)
    _print({k: out[k] for k in ("status", "equity", "open_positions", "kill_switch", "test_mode")})

    print(f"[5/7] daily workflow for {len(holdout)} new sessions via the inbox ...")
    inbox = cfg.path("raw_dir") / "inbox"
    for d in holdout:
        ds = pd.Timestamp(d).strftime("%Y%m%d")
        prices[prices["date"] == d].to_csv(inbox / f"prices_synthetic_{ds}.csv", index=False, date_format="%Y-%m-%d")
        bench[bench["date"] == d].to_csv(inbox / f"benchmarks_synthetic_{ds}.csv", index=False, date_format="%Y-%m-%d")
        r = daily_workflow(cfg, as_of=d)
        print(f"   {pd.Timestamp(d).date()}: {r['status']}" + (f" - {r['error']}" if r.get("error") else ""))
        r2 = daily_workflow(cfg, as_of=d)
        assert r2["status"] == "SKIPPED_ALREADY_COMPLETED", "duplicate-run protection failed"

    print("[6/7] weekly and monthly workflows ...")
    _print(weekly_workflow(cfg, as_of=holdout[-1]))
    _print(monthly_workflow(cfg, as_of=holdout[-1]))
    print("[7/7] done. Launch the dashboard with:")
    print(f"   BQL_ROOT={ws} streamlit run dashboard.py")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Bharat Quant Lab - research & paper trading (no live orders)")
    p.add_argument("--root", help="workspace root (default: project directory)")
    p.add_argument("--config", help="config directory (default: <root>/config)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    s = sub.add_parser("import")
    s.add_argument("--dataset", choices=["prices", "securities", "corporate_actions", "index_membership", "benchmarks",
                                         "holidays", "price_bands"])
    s.add_argument("--file")
    s.add_argument("--inbox", action="store_true", help="import every file in data/raw/inbox")
    s.add_argument("--license-note", default=None)
    s.set_defaults(fn=cmd_import)
    s = sub.add_parser("fetch-exchange", help="download + import official NSE/BSE archive data")
    s.add_argument("--start", help="first date (default: exchange_data.start_date)")
    s.add_argument("--end", help="last date (default: today IST)")
    s.add_argument("--no-download", action="store_true", help="rebuild/import from the local cache only")
    s.set_defaults(fn=cmd_fetch_exchange)
    sub.add_parser("validate-data").set_defaults(fn=cmd_validate)
    s = sub.add_parser("backtest")
    s.add_argument("--strategy", required=True)
    s.add_argument("--params", help='JSON, e.g. \'{"fast": 20, "slow": 100}\'')
    s.add_argument("--start")
    s.add_argument("--end")
    s.set_defaults(fn=cmd_backtest)
    s = sub.add_parser("research")
    s.add_argument("--strategies", help="comma-separated subset")
    s.set_defaults(fn=cmd_research)
    s = sub.add_parser("paper")
    s.add_argument("action", choices=["run", "status", "reset-kill-switch", "reset-strategy"])
    s.add_argument("--as-of")
    s.add_argument("--start", help="first session for a NEW paper portfolio (backfill)")
    s.add_argument("--operator")
    s.add_argument("--note")
    s.add_argument("--strategy")
    s.set_defaults(fn=cmd_paper)
    s = sub.add_parser("report")
    s.add_argument("--period", choices=["daily", "weekly", "monthly", "annual"], default="daily")
    s.add_argument("--as-of")
    s.set_defaults(fn=cmd_report)
    s = sub.add_parser("schedule")
    s.add_argument("workflow", choices=["daily", "weekly", "monthly", "daemon"])
    s.add_argument("--as-of")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_schedule)
    s = sub.add_parser("generate-synthetic")
    s.add_argument("--out", default="data/synthetic")
    s.add_argument("--symbols", type=int, default=60)
    s.add_argument("--seed", type=int, default=7)
    s.set_defaults(fn=cmd_generate_synthetic)
    s = sub.add_parser("demo")
    s.add_argument("--workspace", default=str(ROOT / "demo_workspace"))
    s.add_argument("--fresh", action="store_true")
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--daily-sessions", type=int, default=15)
    s.set_defaults(fn=cmd_demo)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    rc = args.fn(args)
    return int(rc or 0)


if __name__ == "__main__":
    sys.exit(main())
