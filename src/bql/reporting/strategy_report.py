"""Strategy evaluation report (Markdown + HTML + PDF) with the 12 required sections."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .. import PAPER_ONLY_NOTICE
from ..validation.research import StrategyEvaluation
from . import charts
from .exports import to_pdf


def _pct(v) -> str:
    try:
        return f"{float(v):.2%}" if np.isfinite(float(v)) else "n/a"
    except (TypeError, ValueError):
        return "n/a"


def _num(v, d=2) -> str:
    try:
        return f"{float(v):,.{d}f}" if np.isfinite(float(v)) else "n/a"
    except (TypeError, ValueError):
        return "n/a"


def _metrics_table(rows: dict[str, dict]) -> str:
    keys = [("cagr_net", "CAGR (net)", _pct), ("cagr_gross", "CAGR (gross)", _pct),
            ("sharpe_net", "Sharpe (net, excess rf)", _num), ("sortino_net", "Sortino", _num),
            ("calmar", "Calmar", _num), ("max_drawdown", "Max drawdown", _pct),
            ("volatility_ann", "Volatility", _pct), ("cvar_95_daily", "CVaR 95% (daily)", _pct),
            ("trades", "Trades", lambda v: str(v)), ("win_rate", "Win rate", _pct),
            ("profit_factor", "Profit factor", _num), ("payoff_ratio", "Avg win / avg loss", _num),
            ("avg_holding_days", "Avg holding (sessions)", _num), ("market_exposure_avg", "Avg exposure", _pct),
            ("total_charges_inr", "Statutory+brokerage (INR)", lambda v: _num(v, 0)),
            ("total_exec_cost_inr", "Spread/slippage/impact (INR)", lambda v: _num(v, 0)),
            ("benchmark_cagr", "Benchmark CAGR", _pct)]
    cols = list(rows)
    out = ["| Metric | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for k, label, fmt in keys:
        out.append(f"| {label} | " + " | ".join(fmt(rows[c].get(k)) for c in cols) + " |")
    return "\n".join(out)


def render_markdown(e: StrategyEvaluation) -> str:
    d = e.describe
    flags = e.assumptions["data_flags"]
    wm = ""
    if flags.get("synthetic"):
        wm = ("> **SYNTHETIC DATA - SOFTWARE TEST ONLY.** These numbers come from randomly generated prices with no "
              "embedded edge. They are NOT evidence of historical or future profitability.\n\n")
    stats = e.statistics
    wf = e.walk_forward
    lines = [
        f"# Strategy evaluation: `{e.strategy}` ({e.family})", "", wm + f"> {PAPER_ONLY_NOTICE}", "",
        f"**Research status: {e.status}**  |  Score: {e.score['total']}/100", "",
        "Status reasons: " + ("; ".join(e.status_reasons) or "all gates passed"), "",
        "## 1. Research hypothesis", d["hypothesis"], "",
        "## 2. Methodology", d["description"], "", "**Mathematics:** `" + d["mathematics"] + "`", "",
        f"Selected parameters (chosen on TRAINING data only): `{json.dumps(e.selected_params)}`", "",
        "## 3. Data period",
        f"{e.data_period['start']} to {e.data_period['end']} ({e.data_period['sessions']} sessions, first "
        f"{e.data_period['warmup_sessions']} used only as indicator warm-up).", "",
        "| Segment | Start | End |", "|---|---|---|",
        *[f"| {k} | {v['start']} | {v['end']} |" for k, v in e.splits.items()], "",
        f"Data flags: synthetic={flags.get('synthetic')}, point-in-time universe={flags.get('point_in_time_universe')}, "
        f"survivorship-safe={flags.get('survivorship_safe')}", "",
        "## 4. Backtest assumptions",
        f"- Execution: {e.assumptions['execution']}; max {e.assumptions['max_participation']:.0%} of session volume.",
        f"- Costs: {e.assumptions['costs']}.",
        f"- Capital: INR {e.assumptions['initial_capital_inr']:,.0f} simulated; long-only, no leverage.",
        f"- Hard risk limits in research: {e.assumptions['hard_limits_in_research']}.",
        f"- Configuration hash: `{e.assumptions['config_hash']}`.", "",
        "## 5-7. Training, validation and out-of-sample results",
        "Training figures are IN-SAMPLE (parameters were optimised on them) and must not be read as evidence.", "",
        _metrics_table({"Training (in-sample)": e.train, "Validation": e.validation, "Final test (OOS)": e.test}), "",
        f"**Walk-forward (re-optimised each fold, {wf.get('n_folds')} folds, pre-test history only):** "
        f"OOS Sharpe {_num(wf.get('oos_sharpe'))}, OOS CAGR {_pct(wf.get('oos_cagr'))}, OOS max drawdown "
        f"{_pct(wf.get('oos_max_drawdown'))}, positive folds {_pct(wf.get('positive_fold_fraction'))}, "
        f"OOS trades {wf.get('oos_trades')}, parameter changes across folds {wf.get('param_changes')}.", "",
        "| Fold | Train | Test | IS Sharpe | OOS Sharpe | OOS return | OOS MDD |", "|---|---|---|---|---|---|---|",
        *[f"| {f['fold']} | {f['train']} | {f['test']} | {_num(f['is_sharpe'])} | {_num(f['oos_sharpe'])} | "
          f"{_pct(f['oos_return'])} | {_pct(f['oos_mdd'])} |" for f in wf.get("folds", [])], "",
        "## 8. Statistical validation",
        f"- Leakage (causality) test: **{'PASSED' if e.leakage['passed'] else 'FAILED'}** on dates {', '.join(e.leakage['dates_tested'])}.",
        f"- Probabilistic Sharpe Ratio (WF OOS, vs 0 excess): {_pct(stats['psr_wf_oos'])}; final test: {_pct(stats['psr_test'])}.",
        f"- Deflated Sharpe Ratio: {_pct(stats['deflated_sharpe']['dsr'])} (trials = {stats['deflated_sharpe']['n_trials']}, "
        f"expected max Sharpe under null = {_num(stats['deflated_sharpe']['sr0_annualised'])} annualised).",
        f"- Probability of backtest overfitting (CSCV, {stats['pbo'].get('n_combinations')} splits): {_pct(stats['pbo'].get('pbo'))}.",
        f"- Stationary bootstrap Sharpe (WF OOS): 90% CI [{_num(stats['bootstrap_sharpe_wf_oos'].get('ci_low'))}, "
        f"{_num(stats['bootstrap_sharpe_wf_oos'].get('ci_high'))}], P(Sharpe>0) = {_pct(stats['bootstrap_sharpe_wf_oos'].get('p_positive'))}.",
        f"- Monte Carlo (1-year paths): P(loss) = {_pct(stats['monte_carlo_wf_oos'].get('p_loss_1y'))}, "
        f"5th-percentile return {_pct(stats['monte_carlo_wf_oos'].get('return_p05_1y'))}, 95th-percentile drawdown "
        f"{_pct(stats['monte_carlo_wf_oos'].get('mdd_p95'))}.",
        f"- Trade-order Monte Carlo: 95th-percentile losing streak {_num(stats['trade_order_mc'].get('max_losing_streak_p95'), 0)} trades.",
        f"- Return distribution (WF OOS): skew {_num(stats['return_distribution_wf_oos'].get('skew'))}, excess kurtosis "
        f"{_num(stats['return_distribution_wf_oos'].get('excess_kurtosis'))}.", "",
        "### Robustness (pre-test period, selected parameters)", "",
        "| Scenario | Sharpe | CAGR | Max DD | Trades |", "|---|---|---|---|---|",
        *[f"| {k} | {_num(v.get('sharpe'))} | {_pct(v.get('cagr'))} | {_pct(v.get('max_drawdown'))} | {v.get('trades')} |"
          for k, v in e.robustness.items() if isinstance(v, dict) and "sharpe" in v], "",
        f"Parameter neighbourhood: {_pct(e.robustness['parameter_neighbourhood'].get('positive_fraction'))} of the grid had "
        "positive training Sharpe.", "",
        "## 9. Weaknesses", *([f"- {w}" for w in e.weaknesses] or ["- None identified by automated checks."]), "",
        "## 10. Market conditions in which the strategy failed", "",
        "| Regime | Days (WF OOS) | Ann. return | Sharpe |", "|---|---|---|---|",
        *[f"| {k} | {v['days']} | {_pct(v['ann_return'])} | {_num(v['sharpe'])} |"
          for k, v in sorted(e.regimes.get("walk_forward_oos", {}).items())], "",
        "## 11. Risk assessment",
        f"Final-test max drawdown {_pct(e.test.get('max_drawdown'))}; walk-forward OOS max drawdown "
        f"{_pct(wf.get('oos_max_drawdown'))}; daily CVaR(95%) {_pct(e.test.get('cvar_95_daily'))}. "
        f"Hard-limit events that would have triggered in research: see backtest metadata.", "",
        "## 12. Final research status", f"**{e.status}**", "",
        "Gates:", "```", json.dumps(e.gates, indent=1, default=str), "```", "",
        "Passing historical validation is never proof of future profitability.",
    ]
    return "\n".join(lines)


def write_strategy_report(e: StrategyEvaluation, out_dir: Path, benchmark: pd.Series | None = None) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_markdown(e)
    (out_dir / "report.md").write_text(md, encoding="utf-8")
    tr = e.test_result
    eqs = tr.equity.set_index("date")["equity"] if not tr.equity.empty else pd.Series(dtype=float)
    figs = []
    if not tr.equity.empty:
        figs += [charts.equity_curve(tr.equity, benchmark, "Final test period (out-of-sample) - net vs gross"),
                 charts.drawdown_chart(eqs, "Final test drawdown"), charts.monthly_heatmap(eqs)]
    figs.append(charts.returns_curve({"Walk-forward OOS (stitched)": e.curves["walk_forward_oos"],
                                      "Training (IN-SAMPLE)": e.curves["in_sample_train"]},
                                     "Walk-forward out-of-sample vs in-sample (growth of 1)"))
    if len(e.curves["walk_forward_oos"]):
        figs.append(charts.rolling_stats(e.curves["walk_forward_oos"]))
    figs.append(charts.trade_distribution(tr.trades))
    html_path = out_dir / "report.html"
    write_html(md, figs, html_path, f"Strategy evaluation - {e.strategy}")
    pdf_sections = [("Status", {"status": e.status, "score": e.score["total"], "reasons": "; ".join(e.status_reasons)}),
                    ("Hypothesis", e.describe["hypothesis"]), ("Methodology", e.describe["description"]),
                    ("Mathematics", e.describe["mathematics"]), ("Selected parameters", e.selected_params),
                    ("Final test (out-of-sample)", {k: v for k, v in e.test.items() if not isinstance(v, (dict, list))}),
                    ("Walk-forward folds", pd.DataFrame(e.walk_forward.get("folds", []))),
                    ("Robustness", pd.DataFrame({k: v for k, v in e.robustness.items()
                                                 if isinstance(v, dict) and "sharpe" in v}).T.reset_index()),
                    ("Weaknesses", "\n\n".join(e.weaknesses) or "None identified")]
    pdf = to_pdf(f"Strategy evaluation: {e.strategy}", pdf_sections, out_dir / "report.pdf",
                 watermark="SYNTHETIC DATA - SOFTWARE TEST ONLY" if e.assumptions["data_flags"].get("synthetic") else None)
    return {"md": out_dir / "report.md", "html": html_path, "pdf": pdf}


_CSS = """body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;
color:#1b2430;line-height:1.5}table{border-collapse:collapse;margin:8px 0;font-size:13px}th,td{border:1px solid #cdd5df;
padding:4px 8px;text-align:right}th:first-child,td:first-child{text-align:left}th{background:#eef2f6}
blockquote{border-left:4px solid #c2711b;background:#fff7ef;margin:8px 0;padding:6px 12px}code{background:#f2f4f7;padding:1px 4px}
pre{background:#f6f8fa;padding:8px;overflow:auto;font-size:12px}h1{border-bottom:2px solid #1f5fa8}"""


def write_html(md_text: str, figs: list, path: Path, title: str) -> Path:
    from markdown_it import MarkdownIt

    body = MarkdownIt("commonmark").enable("table").render(md_text)
    chart_html = []
    for i, f in enumerate(figs):
        chart_html.append(f.to_html(full_html=False, include_plotlyjs="directory" if i == 0 else False))
    js = path.parent / "plotly.min.js"
    if figs and not js.exists():
        from plotly.offline import get_plotlyjs

        js.write_text(get_plotlyjs(), encoding="utf-8")   # offline: no CDN dependency
    html = (f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title><style>{_CSS}</style></head>"
            f"<body>{body}<h2>Charts</h2>{''.join(chart_html)}</body></html>")
    path.write_text(html, encoding="utf-8")
    return path
