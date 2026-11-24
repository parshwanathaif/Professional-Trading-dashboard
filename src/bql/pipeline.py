"""High-level operations shared by the CLI, the scheduler and the dashboard."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pandas as pd

from .backtesting import ResearchContext, run_backtest
from .backtesting.metrics import max_drawdown, sharpe
from .config import AppConfig
from .data import FileProvider, MarketStore, load_panel_from_store
from .data.schema import DATASETS
from .data.validation import validate_store
from .logging_setup import get_logger, log_event, now_ist
from .portfolio import combine_strategies, diversification_report
from .reporting import write_strategy_report
from .validation import evaluate_strategy, finalize, save_evaluations

log = get_logger("pipeline")


def ensure_dirs(cfg: AppConfig) -> None:
    for name in ("raw_dir", "benchmarks_dir", "corporate_actions_dir", "reference_dir", "reports_dir", "logs_dir",
                 "state_dir"):
        cfg.path(name).mkdir(parents=True, exist_ok=True)
    (cfg.path("raw_dir") / "inbox").mkdir(exist_ok=True)
    (cfg.path("raw_dir") / "archive").mkdir(exist_ok=True)
    cfg.path("market_db").parent.mkdir(parents=True, exist_ok=True)


def import_file(cfg: AppConfig, dataset: str, path: Path, license_note: str | None = None) -> dict:
    store = MarketStore(cfg.path("market_db"))
    kw = {"license_note": license_note} if license_note else {}
    res = FileProvider().fetch(dataset, path=path, **kw)
    return store.ingest(res)


def import_inbox(cfg: AppConfig) -> list[dict]:
    """Import every `<dataset>*.csv|parquet` file in data/raw/inbox, then archive it.

    Files are imported in dependency order (securities/corporate actions before prices).
    """
    inbox = cfg.path("raw_dir") / "inbox"
    archive = cfg.path("raw_dir") / "archive"
    order = ["securities", "corporate_actions", "index_membership", "holidays", "price_bands", "benchmarks", "prices"]
    results = []
    files = sorted(p for p in inbox.glob("*") if p.suffix.lower() in (".csv", ".parquet", ".pq"))
    for ds in order:
        for f in [p for p in files if p.name.lower().startswith(ds)]:
            prov = import_file(cfg, ds, f)
            stamp = now_ist().strftime("%Y%m%dT%H%M%S")
            shutil.move(str(f), archive / f"{stamp}_{f.name}")
            results.append(prov)
    unknown = [p.name for p in files if not any(p.name.lower().startswith(d) for d in DATASETS)]
    if unknown:
        log_event(log, "inbox files with unrecognised names left in place", level=30, files=unknown)
    return results


def fetch_exchange_data(cfg: AppConfig, start=None, end=None, download: bool = True) -> dict:
    """Download official NSE/BSE archive files (resumable), rebuild canonical datasets and import them.

    Raw prices are imported incrementally (only sessions after the latest stored NSE session) and
    are never overwritten. Derived datasets (exchange-implied adjustment factors, validated symbol
    changes, derived delisting dates) are regenerated from the retained raw files and replaced.
    """
    from .data.exchange_sources import (
        LICENSE_NOTE,
        NSE_SOURCE,
        build_datasets,
        cross_check_nse_bse,
        detect_missing_sessions,
        download_all,
        download_corporate_action_reports,
        download_special_sessions,
    )
    from .data.providers import ProviderResult, file_sha256, normalise_frame

    X = cfg.settings.exchange_data
    cache = cfg.root / X.cache_dir
    build = cfg.root / X.build_dir
    start_d = pd.Timestamp(start or X.start_date).date()
    end_d = pd.Timestamp(end).date() if end else now_ist().date()
    stats = {}
    if download:
        bse_start = pd.Timestamp(X.bse_start_date).date() if X.bse_start_date else None
        stats = download_all(cache, start_d, end_d, bse_start, X.min_request_interval_sec)
        stats["corporate_action_reports"] = download_corporate_action_reports(
            cache, start_d - pd.Timedelta(days=35), end_d, max(1.0, X.min_request_interval_sec))
        # sessions held on weekends (Muhurat / special sessions) are found from the bhavcopies themselves
        cons = cfg.root / X.build_dir / "nse_cm_consolidated.parquet"
        if cons.exists():
            missing = detect_missing_sessions(pd.read_parquet(cons, columns=["date", "symbol", "close", "prevclose"]))
            stats["special_sessions"] = download_special_sessions(cache, missing, X.min_request_interval_sec)
    store = MarketStore(cfg.path("market_db"))
    last = store.last_price_date()
    nse_sources = [s for s in store.source_names("prices") if s.startswith(NSE_SOURCE)]
    since = last.date() if (last is not None and nse_sources) else None
    paths = build_datasets(cache, build, tuple(X.nse_series), since=since)
    if download and not (build / "special_sessions_checked").exists():
        # first full build: look for missed weekend sessions, fetch them, rebuild once
        missing = detect_missing_sessions(pd.read_parquet(build / "nse_cm_consolidated.parquet",
                                                          columns=["date", "symbol", "close", "prevclose"]))
        if missing:
            stats["special_sessions"] = download_special_sessions(cache, missing, X.min_request_interval_sec)
            paths = build_datasets(cache, build, tuple(X.nse_series), since=since)
        (build / "special_sessions_checked").write_text(json.dumps([str(d) for d in missing]))

    def result(ds: str, path, name: str):
        frame = normalise_frame(ds, pd.read_parquet(path))
        return ProviderResult(ds, frame, f"{NSE_SOURCE}:{name}", str(path), file_sha256(path), LICENSE_NOTE, False,
                              [f"official exchange archive build; since={since}"])

    imported = {}
    imported["securities"] = store.replace_derived(result("securities", paths["securities"], "securities"),
                                                   "exchange = 'NSE'", [])
    imported["corporate_actions"] = store.replace_derived(
        result("corporate_actions", paths["corporate_actions"], "derived_actions"),
        "exchange = 'NSE' AND (action_type IN ('exchange_adjustment', 'symbol_change', 'bonus', 'split', "
        "'consolidation', 'rights', 'demerger', 'dividend'))", [])
    for ds, name in (("holidays", "holidays"), ("price_bands", "sec_list"), ("benchmarks", "ind_close_all"),
                     ("prices", "cm_bhavcopy")):
        imported[ds] = store.ingest(result(ds, paths[ds], name))
    if "prices_bse" in paths:
        imported["prices_bse"] = store.ingest(result("prices", paths["prices_bse"], "bse_bhavcopy"))
    flagged = cross_check_nse_bse(build)
    summary = json.loads((build / "build_summary.json").read_text())
    out = {"download": stats, "build": summary, "since": str(since) if since else None,
           "imported": {k: {kk: v[kk] for kk in ("rows_in", "rows_inserted", "rows_exact_duplicate", "rows_quarantined")}
                        for k, v in imported.items()},
           "nse_bse_crosscheck": json.loads((build / "nse_bse_crosscheck.json").read_text())
           if (build / "nse_bse_crosscheck.json").exists() else None, "crosscheck_flagged": int(len(flagged)),
           "last_nse_session": str(MarketStore(cfg.path("market_db"), read_only=True).last_price_date())}
    log_event(log, "exchange data fetched", **{k: v for k, v in out.items() if k != "imported"})
    return out


def data_quality(cfg: AppConfig, tag: str = "", as_of=None):
    store = MarketStore(cfg.path("market_db"), read_only=True)
    rep = validate_store(store, cfg, as_of=as_of)
    paths = rep.save(cfg.path("reports_dir") / "data_quality", tag)
    return rep, paths


def research_context(cfg: AppConfig, exclude: list[str] | None = None,
                     exclude_rows: pd.DataFrame | None = None) -> ResearchContext:
    store = MarketStore(cfg.path("market_db"), read_only=True)
    panel = load_panel_from_store(store, cfg, exclude_symbols=exclude, exclude_rows=exclude_rows)
    return ResearchContext.build(cfg, panel)


def run_single_backtest(cfg: AppConfig, strategy: str, params: dict | None = None, start=None, end=None) -> dict:
    rep, _ = data_quality(cfg, "backtest")
    if rep.blocked:
        raise RuntimeError(f"data-quality gate failed: {rep.summary}")
    ctx = research_context(cfg, rep.excluded_symbols, rep.excluded_rows)
    res = run_backtest(ctx, strategy, params, start=start, end=end)
    out = cfg.path("reports_dir") / "backtests" / f"{strategy}_{now_ist():%Y%m%dT%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    res.equity.to_csv(out / "equity.csv", index=False)
    res.trades.to_csv(out / "trades.csv", index=False)
    res.orders.to_csv(out / "orders.csv", index=False)
    res.signals.to_csv(out / "signals.csv", index=False)
    res.fills.to_csv(out / "fills.csv", index=False)
    res.alerts.to_csv(out / "risk_alerts.csv", index=False)
    (out / "metrics.json").write_text(json.dumps({"metrics": res.metrics, "meta": res.run_meta,
                                                  "IMPORTANT": "Full-period single backtest = IN-SAMPLE if parameters "
                                                               "were chosen on this data. Not validated evidence."},
                                                 indent=2, default=str))
    return {"out_dir": out, "metrics": res.metrics, "meta": res.run_meta}


def run_research(cfg: AppConfig, strategies: list[str] | None = None) -> dict:
    rep, dq_paths = data_quality(cfg, "research")
    if rep.blocked:
        raise RuntimeError(f"data-quality gate failed: {rep.summary}")
    ctx = research_context(cfg, rep.excluded_symbols, rep.excluded_rows)
    names = strategies or [n for n, e in cfg.strategies.strategies.items() if e.enabled]
    root = cfg.path("reports_dir") / "research"
    run_id = f"research_{now_ist():%Y%m%dT%H%M%S}"
    evals = []
    for n in names:
        evals.append(evaluate_strategy(ctx, n, test_log=root / "test_evaluations.jsonl"))
    finalize(evals, cfg)
    # multi-strategy diversification analysis on walk-forward OOS streams
    wf = pd.DataFrame({e.strategy: e.curves["walk_forward_oos"] for e in evals}).dropna(how="all")
    portfolio = {}
    if wf.shape[1] >= 2:
        pc = cfg.strategies.portfolio_construction
        for method in ("equal", "inverse_volatility", "risk_parity"):
            port, _ = combine_strategies(wf, method, pc.lookback_days, pc.max_strategy_weight)
            portfolio[method] = {"sharpe": sharpe(port, cfg.settings.backtest.risk_free_rate_annual),
                                 "ann_return": float(port.mean() * 252),
                                 "max_drawdown": max_drawdown((1 + port).cumprod())}
        portfolio["diversification"] = diversification_report(wf)
    out = save_evaluations(evals, root, run_id, extra={
        "data_quality": rep.summary, "excluded_symbols": rep.excluded_symbols, "data_flags": ctx.data_flags,
        "config_hash": cfg.config_hash(), "multi_strategy_wf_oos": portfolio,
    })
    wf.to_parquet(out / "wf_oos_returns.parquet")
    for e in evals:
        write_strategy_report(e, out / e.strategy, ctx.panel.benchmark)
    log_event(log, "research complete", run_id=run_id, statuses={e.strategy: e.status for e in evals})
    return {"run_id": run_id, "out_dir": out, "statuses": {e.strategy: e.status for e in evals},
            "scores": {e.strategy: e.score["total"] for e in evals}, "portfolio": portfolio}
