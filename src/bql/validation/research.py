"""Institutional-style strategy research & validation pipeline.

For every strategy:
  1. Leakage (causality) test.
  2. Parameter grid evaluated on the TRAINING period only -> parameters selected.
  3. Validation period result for the selected parameters (reported, not used for selection).
  4. Rolling walk-forward (re-optimised each fold, pre-test history only) -> stitched OOS returns.
  5. Untouched final TEST period evaluated once with the locked parameters (logged).
  6. Robustness: cost x2/x3, slippage x2/x4, execution delay, missing data, reduced liquidity,
     adverse gaps, parameter neighbourhood.
  7. Statistics: PSR, Deflated Sharpe (all trials across all strategies), stationary bootstrap,
     Monte Carlo, trade-order Monte Carlo, PBO (CSCV), return-distribution tests.
  8. Regime analysis (where it worked / failed).
  9. Multi-dimensional score and research status with explicit gates.
Data limitations (synthetic data, no point-in-time universe) cap the attainable status.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ..backtesting.engine import ResearchContext, config_universe, run_backtest
from ..backtesting.metrics import compute_metrics, max_drawdown, sharpe
from ..logging_setup import get_logger, log_event, now_ist
from ..strategies import make_strategy, param_grid
from .leakage import check_causality
from .splits import chronological_split, walk_forward_folds
from .stats import (
    bootstrap_sharpe,
    deflated_sharpe_ratio,
    monte_carlo_paths,
    parameter_neighbourhood,
    probabilistic_sharpe_ratio,
    probability_of_backtest_overfitting,
    return_distribution,
    sharpe_per_period,
    trade_order_monte_carlo,
)

log = get_logger("validation")

STATUSES = [
    "Rejected",
    "Insufficient evidence",
    "Needs further validation",
    "Passed historical validation",
    "Eligible for paper-trading evaluation",
]
WARMUP_DAYS = 300


@dataclass
class StrategyEvaluation:
    strategy: str
    family: str
    describe: dict
    data_period: dict
    splits: dict
    assumptions: dict
    leakage: dict
    grid: pd.DataFrame
    selected_params: dict
    train: dict
    validation: dict
    test: dict
    walk_forward: dict
    robustness: dict
    statistics: dict
    regimes: dict
    trial_srs: list[float]
    curves: dict = field(default_factory=dict)       # name -> pd.Series
    test_result: object = None
    gates: dict = field(default_factory=dict)
    score: dict = field(default_factory=dict)
    status: str = ""
    status_reasons: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)


def _slice_metrics(eq: pd.DataFrame, trades: pd.DataFrame, start, end, ctx: ResearchContext) -> tuple[dict, pd.Series]:
    e = eq[(eq["date"] >= start) & (eq["date"] <= end)].copy()
    if e.empty:
        return {}, pd.Series(dtype=float)
    scale = e["equity"].iloc[0]
    gscale = e["gross_equity"].iloc[0]
    e0 = e.copy()
    e0["equity"] = e["equity"] / scale * ctx.cfg.settings.backtest.initial_capital_inr
    e0["gross_equity"] = e["gross_equity"] / gscale * ctx.cfg.settings.backtest.initial_capital_inr
    e0["cum_charges"] = e["cum_charges"] - e["cum_charges"].iloc[0]
    e0["cum_exec_cost"] = e["cum_exec_cost"] - e["cum_exec_cost"].iloc[0]
    tr = trades
    if trades is not None and not trades.empty:
        ed = pd.to_datetime(trades["exit_date"])
        tr = trades[(ed >= start) & (ed <= end)]
    m = compute_metrics(e0, tr, ctx.panel.benchmark, ctx.cfg.settings.backtest.risk_free_rate_annual)
    r = e.set_index("date")["equity"].pct_change().dropna()
    return m, r


def _perturb_missing(panel, frac: float, seed: int):
    """Copy of the panel with a random fraction of bars removed (missing-data stress)."""
    rng = np.random.default_rng(seed)
    p = panel.truncate(panel.dates[-1])
    drop = (rng.random(p.close.shape) < frac) & p.available
    for name in ("open", "high", "low", "close", "volume", "value", "raw_close"):
        arr = getattr(p, name)
        arr[drop] = np.nan
    p.available = p.available & ~drop
    return p


def _regime_breakdown(returns: pd.Series, regimes: pd.Series) -> dict:
    if returns.empty:
        return {}
    df = pd.DataFrame({"r": returns, "regime": regimes.reindex(returns.index)}).dropna()
    out = {}
    for reg, g in df.groupby("regime"):
        if len(g) < 20:
            continue
        sd = g["r"].std(ddof=1)
        out[reg] = {"days": int(len(g)), "ann_return": float(g["r"].mean() * 252),
                    "sharpe": float(g["r"].mean() / sd * np.sqrt(252)) if sd > 0 else float("nan"),
                    "hit_rate_days": float((g["r"] > 0).mean())}
    return out


def evaluate_strategy(ctx: ResearchContext, name: str, test_log: Path | None = None) -> StrategyEvaluation:
    cfg = ctx.cfg
    V = cfg.settings.validation
    rf = cfg.settings.backtest.risk_free_rate_annual
    seed = cfg.settings.backtest.random_seed
    entry = cfg.strategies.strategies[name]
    dates = ctx.panel.dates
    sp = chronological_split(dates, V.train_fraction, V.validation_fraction, WARMUP_DAYS)
    pre_test_end = sp["validation"].end
    log_event(log, "evaluating strategy", strategy=name)

    def uni_fn(p):
        return config_universe(p, cfg)

    default = make_strategy(name, entry.params, cfg.risk.position.atr_stop_multiple)
    leak = check_causality(default, ctx.panel, uni_fn, seed=seed)

    # ---------------- grid over pre-test history (one causal run per parameter set)
    grid = param_grid(entry.grid, entry.params)
    runs, rows, signals_cache = [], [], []
    for i, prm in enumerate(grid):
        strat = make_strategy(name, prm, cfg.risk.position.atr_stop_multiple)
        sig = strat.generate(ctx.panel, ctx.universe)
        res = run_backtest(ctx, {name: prm}, signals={name: sig}, end=pre_test_end, record_signals=False)
        eq = res.equity.set_index("date")
        r = eq["equity"].pct_change().fillna(0.0)
        tr = res.trades
        tr_exit = pd.to_datetime(tr["exit_date"]) if not tr.empty else pd.Series(dtype="datetime64[ns]")
        in_train = (tr_exit >= sp["train"].start) & (tr_exit <= sp["train"].end) if not tr.empty else []
        r_train = r[(r.index >= sp["train"].start) & (r.index <= sp["train"].end)]
        n_tr = int(np.sum(in_train)) if len(tr) else 0
        sh_train = sharpe(r_train, rf)
        rows.append({"param_id": i, **{k: prm[k] for k in entry.grid}, "train_sharpe": sh_train,
                     "train_trades": n_tr, "train_cagr": float((1 + r_train).prod() ** (252 / max(len(r_train), 1)) - 1),
                     "sharpe": sh_train})
        runs.append({"params": prm, "returns": r, "equity": res.equity, "trades": tr})
        signals_cache.append(sig)
    grid_df = pd.DataFrame(rows)
    min_tr = max(10, V.min_trades_for_evidence // 3)
    objective = grid_df["train_sharpe"].where(grid_df["train_trades"] >= min_tr, -np.inf).fillna(-np.inf)
    best = int(objective.idxmax()) if np.isfinite(objective.max()) else int(grid_df["train_sharpe"].fillna(-9).idxmax())
    chosen = runs[best]
    sel_params = chosen["params"]

    train_m, _ = _slice_metrics(chosen["equity"], chosen["trades"], sp["train"].start, sp["train"].end, ctx)
    val_m, val_r = _slice_metrics(chosen["equity"], chosen["trades"], sp["validation"].start, sp["validation"].end, ctx)
    is_m, is_r = _slice_metrics(chosen["equity"], chosen["trades"], sp["train"].start, sp["train"].end, ctx)

    # ---------------- walk-forward (pre-test only)
    W = V.walk_forward
    folds = walk_forward_folds(dates, sp["train"].start, pre_test_end, W.train_years, W.test_years, W.step_years)
    oos_parts, fold_rows, oos_trades = [], [], []
    for f in folds:
        best_k, best_sh = 0, -np.inf
        for k, run in enumerate(runs):
            rr = run["returns"]
            sl = rr[(rr.index >= f["train_start"]) & (rr.index <= f["train_end"])]
            s = sharpe(sl, rf)
            if np.isfinite(s) and s > best_sh:
                best_k, best_sh = k, s
        rr = runs[best_k]["returns"]
        oos = rr[(rr.index >= f["test_start"]) & (rr.index <= f["test_end"])]
        oos_parts.append(oos)
        tr = runs[best_k]["trades"]
        if not tr.empty:
            ed = pd.to_datetime(tr["exit_date"])
            oos_trades.append(tr[(ed >= f["test_start"]) & (ed <= f["test_end"])])
        fold_rows.append({"fold": f["fold"], "train": f"{f['train_start'].date()}..{f['train_end'].date()}",
                          "test": f"{f['test_start'].date()}..{f['test_end'].date()}", "param_id": best_k,
                          "is_sharpe": best_sh, "oos_sharpe": sharpe(oos, rf),
                          "oos_return": float((1 + oos).prod() - 1), "oos_mdd": max_drawdown((1 + oos).cumprod())})
    wf_r = pd.concat(oos_parts) if oos_parts else pd.Series(dtype=float)
    wf_trades = pd.concat(oos_trades) if oos_trades else pd.DataFrame()
    wf_eq = (1 + wf_r).cumprod() * cfg.settings.backtest.initial_capital_inr
    fold_df = pd.DataFrame(fold_rows)
    wf = {
        "folds": fold_df.to_dict("records"),
        "oos_sharpe": sharpe(wf_r, rf), "oos_cagr": np.nan,
        "oos_ann_return": float(wf_r.mean() * 252) if len(wf_r) else np.nan,
        "oos_max_drawdown": max_drawdown(wf_eq) if len(wf_eq) else np.nan,
        "positive_fold_fraction": float((fold_df["oos_return"] > 0).mean()) if len(fold_df) else np.nan,
        "oos_trades": int(len(wf_trades)), "n_folds": int(len(fold_df)),
        "param_changes": int((fold_df["param_id"].diff().fillna(0) != 0).sum()) if len(fold_df) else 0,
    }
    if len(wf_eq) > 1:
        yrs = len(wf_r) / 252
        wf["oos_cagr"] = float((wf_eq.iloc[-1] / cfg.settings.backtest.initial_capital_inr) ** (1 / yrs) - 1)

    # ---------------- untouched final test (single evaluation, logged)
    test_res = run_backtest(ctx, {name: sel_params}, signals={name: signals_cache[best]}, start=sp["test"].start,
                            record_signals=True)
    test_m = test_res.metrics
    test_r = test_res.returns.iloc[1:]
    if test_log is not None:
        test_log.parent.mkdir(parents=True, exist_ok=True)
        with open(test_log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts_ist": now_ist().isoformat(timespec="seconds"), "strategy": name,
                                 "params": sel_params, "test": sp["test"].as_dict(),
                                 "config_hash": cfg.config_hash(), "test_sharpe": test_m.get("sharpe_net")},
                                default=str) + "\n")

    # ---------------- robustness (pre-test period, selected params)
    base_sig = {name: signals_cache[best]}
    def _scenario(**kw):
        r = run_backtest(ctx, {name: sel_params}, signals=kw.pop("signals", base_sig), end=pre_test_end,
                         record_signals=False, **kw)
        m = r.metrics
        return {"sharpe": m.get("sharpe_net"), "cagr": m.get("cagr_net"), "max_drawdown": m.get("max_drawdown"),
                "trades": m.get("trades")}
    rob = {"base": _scenario()}
    for mult in V.cost_stress_multipliers[1:]:
        rob[f"costs_x{mult:g}"] = _scenario(cost=ctx.cost.with_stress(cost_multiplier=mult))
    for mult in V.slippage_stress_multipliers[1:]:
        rob[f"slippage_x{mult:g}"] = _scenario(cost=ctx.cost.with_stress(slippage_multiplier=mult))
    for d in V.delay_stress_bars:
        if d != cfg.settings.backtest.execution_delay_bars:
            rob[f"delay_{d}_bars"] = _scenario(delay=d)
    rob["liquidity_participation_1pct"] = _scenario(participation=V.liquidity_stress_participation)
    rob[f"adverse_gap_{V.gap_stress_bps:g}bps"] = _scenario(extra_slippage_bps=V.gap_stress_bps)
    pm = _perturb_missing(ctx.panel, V.missing_data_drop_fraction, seed)
    pm_uni = uni_fn(pm)
    pm_sig = {name: make_strategy(name, sel_params, cfg.risk.position.atr_stop_multiple).generate(pm, pm_uni)}
    r_pm = run_backtest(ctx, {name: sel_params}, signals=pm_sig, end=pre_test_end, record_signals=False,
                        panel_override=pm)
    rob[f"missing_data_{V.missing_data_drop_fraction:.0%}"] = {
        "sharpe": r_pm.metrics.get("sharpe_net"), "cagr": r_pm.metrics.get("cagr_net"),
        "max_drawdown": r_pm.metrics.get("max_drawdown"), "trades": r_pm.metrics.get("trades")}
    rob["parameter_neighbourhood"] = parameter_neighbourhood(grid_df, "train_sharpe")
    if not chosen["trades"].empty:
        rob["consecutive_losses_observed"] = int(
            chosen["trades"]["net_pnl"].le(0).astype(int).groupby(chosen["trades"]["net_pnl"].gt(0).cumsum()).sum().max())

    # ---------------- statistics
    first_active = None
    for run in runs:
        nz = run["returns"][run["returns"] != 0]
        if len(nz):
            first_active = nz.index[0] if first_active is None else min(first_active, nz.index[0])
    mat = pd.DataFrame({k: run["returns"] for k, run in enumerate(runs)})
    if first_active is not None:
        mat = mat[mat.index >= first_active]
    pbo = probability_of_backtest_overfitting(mat, V.pbo_partitions)
    trial_srs = [sharpe_per_period(run["returns"][run["returns"].index >= (first_active or dates[0])])
                 for run in runs]
    statistics = {
        "psr_wf_oos": probabilistic_sharpe_ratio(wf_r - rf / 252),
        "psr_test": probabilistic_sharpe_ratio(test_r - rf / 252),
        "bootstrap_sharpe_wf_oos": bootstrap_sharpe(wf_r - rf / 252, V.bootstrap_samples, V.bootstrap_block_days, seed),
        "monte_carlo_wf_oos": monte_carlo_paths(wf_r, V.monte_carlo_paths, 252, V.bootstrap_block_days, seed),
        "trade_order_mc": trade_order_monte_carlo(wf_trades["net_pnl"] if not wf_trades.empty else [], V.monte_carlo_paths, seed),
        "pbo": pbo,
        "return_distribution_wf_oos": return_distribution(wf_r),
        "trade_return_distribution": (
            {"mean": float(wf_trades["return_pct"].mean()), "median": float(wf_trades["return_pct"].median()),
             "p05": float(wf_trades["return_pct"].quantile(0.05)), "p95": float(wf_trades["return_pct"].quantile(0.95))}
            if not wf_trades.empty else {}),
        "sharpe_stability_fold_std": float(fold_df["oos_sharpe"].std()) if len(fold_df) > 1 else np.nan,
    }
    reg = ctx.regimes["regime"]
    regimes = {"walk_forward_oos": _regime_breakdown(wf_r, reg), "test": _regime_breakdown(test_r, reg),
               "in_sample_train": _regime_breakdown(is_r, reg)}

    strat_obj = make_strategy(name, sel_params)
    ev = StrategyEvaluation(
        strategy=name, family=entry.family, describe=strat_obj.describe(),
        data_period={"start": str(dates[0].date()), "end": str(dates[-1].date()), "sessions": int(len(dates)),
                     "warmup_sessions": WARMUP_DAYS},
        splits={k: v.as_dict() for k, v in sp.items()},
        assumptions={
            "initial_capital_inr": cfg.settings.backtest.initial_capital_inr,
            "execution": f"signal at close t, fill at {cfg.settings.backtest.fill_price} of t+{cfg.settings.backtest.execution_delay_bars}",
            "max_participation": cfg.settings.backtest.max_participation_of_volume,
            "costs": "date-effective statutory charges (config/transaction_costs.yaml) + spread + slippage + sqrt impact",
            "risk_limits": cfg.risk.model_dump(), "config_hash": cfg.config_hash(), "data_flags": ctx.data_flags,
            "hard_limits_in_research": "logged, not enforced (see RISK_METHODOLOGY.md)",
        },
        leakage=leak, grid=grid_df, selected_params=sel_params, train=train_m, validation=val_m, test=test_m,
        walk_forward=wf, robustness=rob, statistics=statistics, regimes=regimes, trial_srs=trial_srs,
        curves={"in_sample_train": is_r, "validation": val_r, "walk_forward_oos": wf_r, "test": test_r},
        test_result=test_res,
    )
    return ev


# ============================================================================ gating & scoring
def _f(x, default=np.nan) -> float:
    try:
        v = float(x)
        return v if np.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def finalize(evals: list[StrategyEvaluation], cfg) -> list[StrategyEvaluation]:
    G = cfg.settings.validation.gates
    V = cfg.settings.validation
    rf = cfg.settings.backtest.risk_free_rate_annual
    all_srs = [s for e in evals for s in e.trial_srs if np.isfinite(s)]
    n_trials = len(all_srs)
    for e in evals:
        wf_r = e.curves["walk_forward_oos"]
        dsr, sr0 = deflated_sharpe_ratio(wf_r - rf / 252, all_srs, n_trials)
        e.statistics["deflated_sharpe"] = {"dsr": dsr, "sr0_per_period": sr0, "sr0_annualised": sr0 * np.sqrt(252),
                                           "n_trials": n_trials}
        wf_sh = _f(e.walk_forward.get("oos_sharpe"))
        test_sh = _f(e.test.get("sharpe_net"))
        pbo = _f(e.statistics["pbo"].get("pbo"))
        cost2 = _f(e.robustness.get("costs_x2", {}).get("sharpe"))
        base = _f(e.robustness["base"].get("sharpe"))
        mdd = min(_f(e.walk_forward.get("oos_max_drawdown"), 0), _f(e.test.get("max_drawdown"), 0))
        trades = int(e.walk_forward.get("oos_trades", 0)) + int(e.test.get("trades", 0) or 0)
        pos_folds = _f(e.walk_forward.get("positive_fold_fraction"), 0)
        neigh = _f(e.robustness["parameter_neighbourhood"].get("positive_fraction"), 0)
        rob_sh = [_f(v.get("sharpe")) for k, v in e.robustness.items() if isinstance(v, dict) and "sharpe" in v]
        dsr_v = _f(dsr, 0)
        critical = {
            "leakage_test_passed": bool(e.leakage["passed"]),
            "wf_oos_sharpe_positive": wf_sh > 0,
            "test_sharpe_positive": test_sh > 0,
            "pbo_below_max": (pbo <= G.max_pbo) if np.isfinite(pbo) else False,
            "survives_2x_costs": cost2 > G.min_cost_stress_sharpe,
        }
        standard = {
            "wf_oos_sharpe_min": wf_sh >= G.min_oos_sharpe,
            "dsr_min": dsr_v >= G.min_dsr,
            "drawdown_within_limit": mdd >= -G.max_oos_drawdown,
            "positive_wf_folds": pos_folds >= G.min_positive_wf_fold_fraction,
            "parameter_plateau": neigh >= G.min_param_neighbourhood_positive,
        }
        eligible = {"dsr_eligible": dsr_v >= G.eligible_min_dsr,
                    "all_robustness_scenarios_positive": all(np.isfinite(s) and s > 0 for s in rob_sh)}
        e.gates = {"critical": critical, "standard": standard, "eligibility": eligible,
                   "trades_evaluated": trades, "min_trades": V.min_trades_for_evidence}
        reasons = []
        if not all(critical.values()):
            status = "Rejected"
            reasons += [f"failed critical gate: {k}" for k, v in critical.items() if not v]
        elif trades < V.min_trades_for_evidence:
            status = "Insufficient evidence"
            reasons.append(f"only {trades} out-of-sample trades (< {V.min_trades_for_evidence})")
        elif not all(standard.values()):
            status = "Needs further validation"
            reasons += [f"failed gate: {k}" for k, v in standard.items() if not v]
        elif not all(eligible.values()):
            status = "Passed historical validation"
            reasons += [f"not yet eligible: {k}" for k, v in eligible.items() if not v]
        else:
            status = "Eligible for paper-trading evaluation"
        flags = e.assumptions["data_flags"]
        if status != "Rejected":
            if flags.get("synthetic"):
                if STATUSES.index(status) > STATUSES.index("Insufficient evidence"):
                    reasons.append(f"capped from '{status}': SYNTHETIC test data is not evidence")
                status = "Insufficient evidence"
            elif not flags.get("survivorship_safe"):
                if STATUSES.index(status) > STATUSES.index("Needs further validation"):
                    reasons.append(f"capped from '{status}': universe not point-in-time / survivorship-safe")
                    status = "Needs further validation"
        e.status, e.status_reasons = status, reasons

        # --- score (0-100)
        def clip01(x):
            return float(np.clip(x, 0, 1)) if np.isfinite(x) else 0.0
        regs = e.regimes.get("walk_forward_oos", {})
        reg_pos = np.mean([v["ann_return"] > 0 for v in regs.values()]) if regs else 0.0
        comp = {
            "out_of_sample_performance": clip01((wf_sh + 0.5) / 1.5),
            "final_test_performance": clip01((test_sh + 0.5) / 1.5),
            "drawdown": clip01(1 - abs(mdd) / 0.5),
            "risk_adjusted_return": clip01((_f(e.test.get("sortino_net"), -1) + 0.5) / 2.0),
            "consistency": clip01(pos_folds),
            "sample_size": clip01(trades / 200),
            "cost_sensitivity": clip01(cost2 / base) if base > 0 and np.isfinite(cost2) else 0.0,
            "parameter_stability": clip01(neigh),
            "regime_robustness": clip01(reg_pos),
            "liquidity": clip01(_f(e.robustness.get("liquidity_participation_1pct", {}).get("sharpe"), -1) /
                                base) if base > 0 else 0.0,
            "statistical_confidence": clip01(dsr_v),
        }
        weights = {"out_of_sample_performance": 0.15, "final_test_performance": 0.10, "drawdown": 0.10,
                   "risk_adjusted_return": 0.05, "consistency": 0.10, "sample_size": 0.05, "cost_sensitivity": 0.10,
                   "parameter_stability": 0.10, "regime_robustness": 0.10, "liquidity": 0.05,
                   "statistical_confidence": 0.10}
        e.score = {"total": round(100 * sum(comp[k] * w for k, w in weights.items()), 1),
                   "components": {k: round(v, 3) for k, v in comp.items()}, "weights": weights}

        # --- weaknesses (plain English)
        w = []
        if not e.leakage["passed"]:
            w.append("Leakage test FAILED - results invalid until fixed.")
        if wf_sh <= 0:
            w.append(f"Walk-forward out-of-sample Sharpe (excess of {rf:.1%} risk-free) is {wf_sh:.2f}.")
        if np.isfinite(pbo) and pbo > 0.3:
            w.append(f"Probability of backtest overfitting is {pbo:.0%}: the in-sample best parameters often "
                     "underperform out of sample.")
        if base > 0 and cost2 < 0.5 * base:
            w.append("Performance is highly sensitive to transaction costs.")
        if neigh < 0.6:
            w.append(f"Only {neigh:.0%} of the parameter grid had positive training Sharpe (isolated optimum).")
        bad = [k for k, v in regs.items() if v["ann_return"] < 0]
        if bad:
            w.append("Lost money out-of-sample in regimes: " + ", ".join(sorted(bad)) + ".")
        if abs(mdd) > G.max_oos_drawdown:
            w.append(f"Out-of-sample drawdown {mdd:.1%} exceeds the {G.max_oos_drawdown:.0%} limit.")
        if flags.get("synthetic"):
            w.append("Evaluated on SYNTHETIC data - says nothing about real-market behaviour.")
        if not flags.get("survivorship_safe"):
            w.append("Universe is not point-in-time: survivorship bias likely inflates results.")
        e.weaknesses = w
    return evals


# ============================================================================ persistence
def _jsonable(o):
    if isinstance(o, pd.DataFrame):
        return o.to_dict("records")
    if isinstance(o, pd.Series):
        return {str(k): v for k, v in o.items()}
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, pd.Timestamp):
        return str(o.date())
    return str(o)


def evaluation_to_dict(e: StrategyEvaluation) -> dict:
    return {
        "strategy": e.strategy, "family": e.family, "status": e.status, "status_reasons": e.status_reasons,
        "score": e.score, "describe": e.describe, "data_period": e.data_period, "splits": e.splits,
        "assumptions": e.assumptions, "leakage": e.leakage, "selected_params": e.selected_params,
        "grid": e.grid.to_dict("records"), "train": e.train, "validation": e.validation, "test": e.test,
        "walk_forward": e.walk_forward, "robustness": e.robustness, "statistics": e.statistics,
        "regimes": e.regimes, "gates": e.gates, "weaknesses": e.weaknesses,
    }


def save_evaluations(evals: list[StrategyEvaluation], out_root: Path, run_id: str, extra: dict | None = None) -> Path:
    out = out_root / run_id
    out.mkdir(parents=True, exist_ok=True)
    summary = []
    for e in evals:
        d = out / e.strategy
        d.mkdir(exist_ok=True)
        (d / "evaluation.json").write_text(json.dumps(evaluation_to_dict(e), indent=2, default=_jsonable))
        curves = pd.DataFrame({k: v for k, v in e.curves.items()})
        curves.index.name = "date"
        curves.to_parquet(d / "curves.parquet")
        tr = e.test_result
        tr.equity.to_parquet(d / "test_equity.parquet")
        tr.trades.to_csv(d / "test_trades.csv", index=False)
        tr.orders.to_csv(d / "test_orders.csv", index=False)
        tr.signals.to_csv(d / "test_signals.csv", index=False)
        summary.append({
            "strategy": e.strategy, "family": e.family, "status": e.status, "score": e.score["total"],
            "params": json.dumps(e.selected_params, default=str),
            "wf_oos_sharpe": e.walk_forward.get("oos_sharpe"), "wf_oos_cagr": e.walk_forward.get("oos_cagr"),
            "test_sharpe": e.test.get("sharpe_net"), "test_cagr_net": e.test.get("cagr_net"),
            "test_cagr_gross": e.test.get("cagr_gross"), "test_mdd": e.test.get("max_drawdown"),
            "test_trades": e.test.get("trades"), "dsr": e.statistics["deflated_sharpe"]["dsr"],
            "pbo": e.statistics["pbo"].get("pbo"), "leakage_passed": e.leakage["passed"],
            "reasons": "; ".join(e.status_reasons),
        })
    sdf = pd.DataFrame(summary).sort_values("score", ascending=False)
    sdf.to_csv(out / "summary.csv", index=False)
    meta = {"run_id": run_id, "created_ist": now_ist().isoformat(timespec="seconds"),
            "statuses": {r["strategy"]: r["status"] for r in summary}, **(extra or {})}
    (out / "run_meta.json").write_text(json.dumps(meta, indent=2, default=_jsonable))
    (out_root / "latest.json").write_text(json.dumps(meta, indent=2, default=_jsonable))
    return out


def latest_statuses(research_root: Path) -> dict[str, str]:
    p = research_root / "latest.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text()).get("statuses", {})
