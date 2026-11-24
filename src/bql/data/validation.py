"""Data-quality engine. Runs before every backtest, research run and paper-trading run.

Severity
--------
CRITICAL  dataset-level -> the run is blocked.
CRITICAL  symbol-level  -> the symbol is excluded from the run (recorded in the report).
WARNING   reported; does not block. (e.g. possible special-session bar, stale prices)
INFO      coverage statistics.

Nothing is repaired or filled by this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ..logging_setup import now_ist
from ..market_calendar import TradingCalendar


@dataclass
class DataQualityReport:
    issues: pd.DataFrame
    summary: dict
    excluded_symbols: list[str] = field(default_factory=list)
    blocked: bool = False
    excluded_rows: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=["date", "symbol"]))
    generated_at_ist: str = field(default_factory=lambda: now_ist().isoformat(timespec="seconds"))

    @property
    def passed(self) -> bool:
        return not self.blocked

    def counts(self) -> dict:
        if self.issues.empty:
            return {}
        return self.issues.groupby(["severity", "check"]).size().rename("n").reset_index().to_dict("records")

    def save(self, out_dir: Path, tag: str = "") -> dict[str, Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = now_ist().strftime("%Y%m%dT%H%M%S")
        base = out_dir / f"data_quality_{tag + '_' if tag else ''}{stamp}"
        self.issues.to_csv(f"{base}_issues.csv", index=False)
        meta = {
            "generated_at_ist": self.generated_at_ist,
            "blocked": self.blocked,
            "excluded_symbols": self.excluded_symbols,
            "summary": self.summary,
            "counts": self.counts(),
        }
        Path(f"{base}.json").write_text(json.dumps(meta, indent=2, default=str))
        html = [
            "<html><head><meta charset='utf-8'><title>Data quality report</title></head><body>",
            "<h1>Data Quality Report</h1>",
            f"<p>Generated (IST): {self.generated_at_ist}</p>",
            f"<p><b>Status: {'BLOCKED' if self.blocked else 'PASSED'}</b></p>",
            "<h2>Summary</h2><pre>" + json.dumps(self.summary, indent=2, default=str) + "</pre>",
            f"<h2>Excluded symbols ({len(self.excluded_symbols)})</h2><p>{', '.join(self.excluded_symbols) or 'None'}</p>",
            "<h2>Issues (first 500)</h2>",
            self.issues.head(500).to_html(index=False),
            "</body></html>",
        ]
        Path(f"{base}.html").write_text("\n".join(html), encoding="utf-8")
        return {"csv": Path(f"{base}_issues.csv"), "json": Path(f"{base}.json"), "html": Path(f"{base}.html")}


def _longest_run(pos: np.ndarray) -> int:
    """Length of the longest run of consecutive integers in a sorted array."""
    if len(pos) == 0:
        return 0
    breaks = np.flatnonzero(np.diff(pos) != 1)
    starts = np.r_[0, breaks + 1]
    ends = np.r_[breaks, len(pos) - 1]
    return int(np.max(ends - starts + 1))


def _issue(rows: list, severity, check, scope, symbol, date, detail):
    rows.append(
        {"severity": severity, "check": check, "scope": scope, "symbol": symbol, "date": date, "detail": detail}
    )


def validate_prices(
    prices: pd.DataFrame,
    benchmark: pd.DataFrame | None = None,
    securities: pd.DataFrame | None = None,
    actions: pd.DataFrame | None = None,
    calendar: TradingCalendar | None = None,
    max_abs_return_flag: float = 0.25,
    max_missing_ratio: float = 0.05,
    as_of: pd.Timestamp | None = None,
    quarantine_count: int = 0,
    exclude_symbols_with_critical: bool = True,
    max_gap_bars: int = 20,
    critical_row_policy: str = "exclude_symbol",
    gaps_are_non_trading: bool = False,
) -> DataQualityReport:
    """critical_row_policy='exclude_rows': an invalid row (bad OHLC, non-positive price, negative volume)
    is excluded on its own (treated as no bar) instead of excluding the whole symbol.
    gaps_are_non_trading=True: the price source is the exchange's complete daily file, so a symbol
    missing on a session genuinely did not trade (suspension/illiquidity). Gaps are reported but
    do not exclude the symbol - excluding suspended stocks would itself create survivorship bias."""
    rows: list[dict] = []
    as_of = pd.Timestamp(as_of or now_ist().date())
    p = prices.copy()

    if p.empty:
        _issue(rows, "CRITICAL", "no_price_data", "dataset", None, None, "no price rows available")
        return DataQualityReport(pd.DataFrame(rows), {"rows": 0}, [], True)

    # ---- dataset-level checks
    if benchmark is None or benchmark.empty:
        _issue(rows, "CRITICAL", "benchmark_missing", "dataset", None, None,
               "benchmark index series missing: regime detection and benchmark comparison impossible")
    if quarantine_count:
        _issue(rows, "WARNING", "quarantined_rows", "dataset", None, None,
               f"{quarantine_count} conflicting rows held in quarantine (not used)")
    dup = p.duplicated(subset=["date", "symbol", "exchange"], keep=False)
    for r in p[dup].itertuples():
        _issue(rows, "CRITICAL", "duplicate_key", "symbol", r.symbol, r.date, "duplicate date/symbol row")

    future = p["date"] > as_of
    for r in p[future].itertuples():
        _issue(rows, "CRITICAL", "future_date", "symbol", r.symbol, r.date, "date after as-of date")

    # ---- row validity
    o, h, lo, c, v = (p[k] for k in ("open", "high", "low", "close", "volume"))
    nan_px = o.isna() | h.isna() | lo.isna() | c.isna()
    nonpos = (o <= 0) | (h <= 0) | (lo <= 0) | (c <= 0)
    bad_hl = (h < lo) | (h < o) | (h < c) | (lo > o) | (lo > c)
    neg_vol = v < 0
    row_scope = "row" if critical_row_policy == "exclude_rows" else "symbol"
    bad_row_mask = pd.Series(False, index=p.index)
    for mask, name, detail in [
        (nan_px, "missing_ohlc_value", "one or more OHLC values missing"),
        (nonpos & ~nan_px, "non_positive_price", "price <= 0"),
        (bad_hl & ~nan_px, "invalid_ohlc", "high/low inconsistent with open/close"),
        (neg_vol, "negative_volume", "volume < 0"),
    ]:
        bad_row_mask |= mask.fillna(False)
        for r in p[mask].itertuples():
            _issue(rows, "CRITICAL", name, row_scope, r.symbol, r.date, detail)
    zero_vol = (v == 0) & ~nan_px
    for r in p[zero_vol].itertuples():
        _issue(rows, "WARNING", "zero_volume", "symbol", r.symbol, r.date, "zero volume (possible halt/suspension)")

    # weekend / holiday bars
    if calendar is not None:
        non_td = ~p["date"].map(calendar.is_trading_day)
        for r in p[non_td].itertuples():
            _issue(rows, "WARNING", "non_trading_day_bar", "symbol", r.symbol, r.date,
                   "bar on weekend/holiday: verify (special/Muhurat session?)")

    # ---- per-symbol continuity checks against reference calendar
    ref_dates = None
    if benchmark is not None and not benchmark.empty:
        ref_dates = pd.DatetimeIndex(sorted(benchmark["date"].unique()))
    split_dates = set()
    if actions is not None and not actions.empty:
        adj = actions[actions["action_type"].isin(["split", "bonus", "rights", "demerger", "merger",
                                                    "exchange_adjustment", "consolidation"])]
        split_dates = set(zip(adj["symbol"], pd.to_datetime(adj["ex_date"])))
    delist = {}
    if securities is not None and not securities.empty:
        delist = dict(zip(securities["symbol"], securities["delisting_date"]))

    coverage = {}
    for sym, g in p.sort_values("date").groupby("symbol"):
        d = pd.DatetimeIndex(g["date"])
        first, last = d.min(), d.max()
        if ref_dates is not None:
            end = last
            dl = delist.get(sym)
            if dl is not None and pd.notna(dl):
                end = min(end, pd.Timestamp(dl))
            expected = ref_dates[(ref_dates >= first) & (ref_dates <= end)]
            missing = expected.difference(d)
            ratio = len(missing) / max(len(expected), 1)
            coverage[sym] = round(1 - ratio, 4)
            if len(missing):
                pos = np.flatnonzero(~expected.isin(d))
                longest = _longest_run(pos)
                sev = "CRITICAL" if (ratio > max_missing_ratio or longest > max_gap_bars) else "WARNING"
                if gaps_are_non_trading:
                    sev = "INFO" if sev == "WARNING" else "WARNING"
                if longest > max_gap_bars:
                    _issue(rows, "WARNING" if gaps_are_non_trading else "CRITICAL", "long_data_gap", "symbol", sym,
                           missing[0], f"longest run of consecutive missing bars = {longest} (> {max_gap_bars})"
                           + ("; exchange reported no trades (suspension/illiquidity)" if gaps_are_non_trading else ""))
                _issue(rows, sev, "missing_bars", "symbol", sym, missing[0],
                       f"{len(missing)} missing bars ({ratio:.2%}) between {first.date()} and {end.date()}")
        ret = g["close"].pct_change()
        big = ret.abs() > max_abs_return_flag
        for dt, rv in zip(g.loc[big, "date"], ret[big]):
            explained = (sym, pd.Timestamp(dt)) in split_dates
            _issue(rows, "INFO" if explained else "WARNING", "extreme_move", "symbol", sym, dt,
                   f"close-to-close move {rv:+.1%}" + (" (explained by corporate action)" if explained else
                                                       " - unexplained; verify against exchange records"))
        stale = g["close"].diff().eq(0).astype(int)
        run = stale.groupby((stale == 0).cumsum()).cumsum()
        if (run >= 5).any():
            dt = g.loc[run >= 5, "date"].iloc[0]
            _issue(rows, "WARNING", "stale_price", "symbol", sym, dt, "close unchanged for >=5 consecutive bars")

    issues = pd.DataFrame(rows, columns=["severity", "check", "scope", "symbol", "date", "detail"])
    sym_crit = issues[(issues["severity"] == "CRITICAL") & (issues["scope"] == "symbol")]["symbol"].dropna()
    excluded = sorted(set(sym_crit)) if exclude_symbols_with_critical else []
    blocked = bool(((issues["severity"] == "CRITICAL") & (issues["scope"] == "dataset")).any())
    if not exclude_symbols_with_critical and len(sym_crit):
        blocked = True
    excluded_rows = (p.loc[bad_row_mask, ["date", "symbol"]].reset_index(drop=True)
                     if row_scope == "row" else pd.DataFrame(columns=["date", "symbol"]))
    summary = {
        "rows": int(len(p)),
        "excluded_rows": int(len(excluded_rows)),
        "symbols": int(p["symbol"].nunique()),
        "date_min": str(p["date"].min().date()),
        "date_max": str(p["date"].max().date()),
        "critical": int((issues["severity"] == "CRITICAL").sum()),
        "warning": int((issues["severity"] == "WARNING").sum()),
        "info": int((issues["severity"] == "INFO").sum()),
        "excluded_symbols": len(excluded),
        "median_coverage": float(np.median(list(coverage.values()))) if coverage else None,
        "benchmark_rows": 0 if benchmark is None else int(len(benchmark)),
    }
    return DataQualityReport(issues, summary, excluded, blocked, excluded_rows=excluded_rows)


def validate_store(store, cfg, as_of=None, symbols: list[str] | None = None) -> DataQualityReport:
    """Run the configured data-quality gate on the NSE price data in a MarketStore."""
    from ..market_calendar import TradingCalendar

    d = cfg.settings.data
    if symbols is None and d.prefilter_liquid_symbols:
        symbols = store.liquid_symbols(d.min_median_traded_value_inr)
    prices = store.prices(exchange="NSE", symbols=symbols)
    bench = store.benchmark(cfg.settings.backtest.benchmark)
    if as_of is not None:
        prices = prices[prices["date"] <= pd.Timestamp(as_of)]
        bench = bench[bench["date"] <= pd.Timestamp(as_of)]
    secs = store.table("securities")
    secs = secs[secs["exchange"] == "NSE"] if "exchange" in secs else secs
    return validate_prices(
        prices, bench, secs, store.table("corporate_actions"), TradingCalendar.from_store(store),
        d.max_abs_daily_return_flag, d.max_missing_ratio, as_of=pd.Timestamp(as_of) if as_of else None,
        quarantine_count=len(store.table("quarantine")),
        exclude_symbols_with_critical=d.exclude_symbols_with_critical_issues, max_gap_bars=d.max_gap_bars,
        critical_row_policy=d.critical_row_policy, gaps_are_non_trading=d.gaps_are_non_trading)
