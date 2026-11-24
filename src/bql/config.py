"""Configuration loading, validation and versioning.

All YAML configuration is validated with Pydantic. Each loaded configuration bundle has a
deterministic SHA-256 hash that is stored with every backtest, research run and paper-trading
decision so that results can be traced back to the exact assumptions used.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
IST = "Asia/Kolkata"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- settings
class ProjectCfg(_Strict):
    name: str
    timezone: str = IST
    mode: str = "offline_csv"

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        if v != IST:
            raise ValueError("timezone must be Asia/Kolkata for Indian equity research")
        return v


class PathsCfg(_Strict):
    market_db: str
    paper_db: str
    raw_dir: str
    benchmarks_dir: str
    corporate_actions_dir: str
    reference_dir: str
    reports_dir: str
    logs_dir: str
    state_dir: str

    def resolve(self, name: str, root: Path | None = None) -> Path:
        p = Path(getattr(self, name))
        return p if p.is_absolute() else (root or PROJECT_ROOT) / p


class DataCfg(_Strict):
    max_abs_daily_return_flag: float = 0.25
    max_missing_ratio: float = 0.05
    exclude_symbols_with_critical_issues: bool = True
    min_median_traded_value_inr: float = 5e7
    min_price_inr: float = 20.0
    min_history_days: int = 252
    default_benchmark: str = "NIFTY50"
    universe_index: str | None = None   # point-in-time index membership defining the universe
    universe_mode: str = "index_membership"   # index_membership | liquidity_top_n | all
    universe_top_n: int | None = None
    universe_rank_window: int = 126
    prefilter_liquid_symbols: bool = True
    equity_isin_prefixes: list[str] | None = None   # e.g. ["INE"]: company shares only (no ETFs/funds)
    critical_row_policy: str = "exclude_symbol"   # exclude_symbol | exclude_rows
    gaps_are_non_trading: bool = False            # True for complete exchange files: a gap = no trades
    max_gap_bars: int = 20

    @field_validator("universe_mode")
    @classmethod
    def _um(cls, v: str) -> str:
        if v not in {"index_membership", "liquidity_top_n", "all"}:
            raise ValueError("universe_mode must be index_membership, liquidity_top_n or all")
        return v


class BacktestCfg(_Strict):
    initial_capital_inr: float = Field(1e7, gt=0)
    execution_delay_bars: int = Field(1, ge=1)  # >=1 enforces no same-bar execution
    fill_price: str = "open"
    max_participation_of_volume: float = Field(0.05, gt=0, le=1)
    delisting_haircut: float = Field(0.30, ge=0, le=1)
    stale_price_mark_days: int = 5
    benchmark: str = "NIFTY50"
    risk_free_rate_annual: float = 0.065
    random_seed: int = 20240601

    @field_validator("fill_price")
    @classmethod
    def _fp(cls, v: str) -> str:
        if v not in {"open", "close"}:
            raise ValueError("fill_price must be 'open' or 'close'")
        return v


class WalkForwardCfg(_Strict):
    train_years: int = 4
    test_years: int = 1
    step_years: int = 1


class GatesCfg(_Strict):
    min_oos_sharpe: float = 0.30
    min_dsr: float = 0.90
    max_pbo: float = 0.50
    max_oos_drawdown: float = 0.35
    min_cost_stress_sharpe: float = 0.0
    min_positive_wf_fold_fraction: float = 0.5
    min_param_neighbourhood_positive: float = 0.6
    eligible_min_dsr: float = 0.95


class ValidationCfg(_Strict):
    train_fraction: float = 0.6
    validation_fraction: float = 0.2
    walk_forward: WalkForwardCfg = WalkForwardCfg()
    min_trades_for_evidence: int = 30
    bootstrap_samples: int = 1000
    bootstrap_block_days: int = 20
    monte_carlo_paths: int = 1000
    pbo_partitions: int = 10
    cost_stress_multipliers: list[float] = [1.0, 2.0, 3.0]
    slippage_stress_multipliers: list[float] = [1.0, 2.0, 4.0]
    delay_stress_bars: list[int] = [1, 2, 3]
    missing_data_drop_fraction: float = 0.02
    liquidity_stress_participation: float = 0.01
    gap_stress_bps: float = 150
    gates: GatesCfg = GatesCfg()


class SelectionCfg(_Strict):
    # status_only:   only strategies whose status is in allowed_statuses may open positions
    # best_evidence: additionally the top `max_strategies` by research score whose status is at
    #                least `min_status` (never a Rejected strategy)
    mode: str = "status_only"
    max_strategies: int = Field(2, ge=1, le=6)
    min_status: str = "Needs further validation"

    @field_validator("mode")
    @classmethod
    def _m(cls, v: str) -> str:
        if v not in {"status_only", "best_evidence"}:
            raise ValueError("mode must be status_only or best_evidence")
        return v

    @field_validator("min_status")
    @classmethod
    def _s(cls, v: str) -> str:
        allowed = ["Insufficient evidence", "Needs further validation", "Passed historical validation",
                   "Eligible for paper-trading evaluation"]
        if v not in allowed:
            raise ValueError(f"min_status must be one of {allowed} (a Rejected strategy can never be selected)")
        return v


class CallsCfg(_Strict):
    enabled: bool = True
    your_capital_inr: float = Field(1_000_000, gt=0)   # used only to translate weights into quantities


class ShadowCfg(_Strict):
    """Owner opt-in shadow book: tracks the top research-score strategy regardless of status.
    Calls from it are labelled 'SHADOW - validation-rejected, track only'."""
    enabled: bool = False
    portfolio_name: str = "shadow_book"
    max_strategies: int = Field(1, ge=1, le=3)


class PaperCfg(_Strict):
    portfolio_name: str = "core_paper"
    initial_capital_inr: float = 1e7
    allowed_statuses: list[str] = ["Eligible for paper-trading evaluation"]
    test_mode_allow_unvalidated: bool = False
    strategy_selection: SelectionCfg = SelectionCfg()
    calls: CallsCfg = CallsCfg()
    shadow: ShadowCfg = ShadowCfg()


class SchedulerCfg(_Strict):
    market_close_ist: str = "15:30"
    daily_run_after_ist: str = "18:30"
    lock_stale_after_minutes: int = 180
    max_retries: int = 2


class ExchangeDataCfg(_Strict):
    enabled: bool = False
    start_date: str = "2013-01-01"
    nse_series: list[str] = ["EQ", "BE", "BZ"]
    bse_start_date: str | None = "2024-01-01"
    min_request_interval_sec: float = Field(0.35, ge=0.25)   # politeness floor, cannot be configured lower
    auto_fetch_in_daily_workflow: bool = True
    cache_dir: str = "data/raw/exchange"
    build_dir: str = "data/processed/exchange_build"


class Settings(_Strict):
    project: ProjectCfg
    paths: PathsCfg
    data: DataCfg = DataCfg()
    backtest: BacktestCfg = BacktestCfg()
    validation: ValidationCfg = ValidationCfg()
    paper_trading: PaperCfg = PaperCfg()
    scheduler: SchedulerCfg = SchedulerCfg()
    exchange_data: ExchangeDataCfg = ExchangeDataCfg()


# --------------------------------------------------------------------------- risk
class PositionLimits(_Strict):
    risk_per_position: float = Field(0.0075, gt=0, le=0.05)
    max_position_weight: float = Field(0.08, gt=0, le=0.5)
    max_open_positions: int = Field(20, ge=1)
    atr_stop_multiple: float = Field(2.5, gt=0)
    max_holding_days: int = 250
    min_position_value_inr: float = 25000


class PortfolioLimits(_Strict):
    max_gross_exposure: float = Field(1.0, gt=0, le=1.0)  # no leverage permitted
    max_sector_exposure: float = Field(0.25, gt=0, le=1.0)
    max_avg_pairwise_correlation: float = 0.75
    correlation_lookback_days: int = 120
    volatility_target_annual: float = 0.15
    volatility_lookback_days: int = 63
    min_cash_buffer: float = 0.02


class LiquidityLimits(_Strict):
    max_adv_participation_at_sizing: float = 0.02


class LossLimits(_Strict):
    max_daily_loss: float = 0.03
    max_weekly_loss: float = 0.06
    max_drawdown_warning: float = 0.10
    max_drawdown_kill: float = 0.20
    max_consecutive_losses: int = 8


class GapRisk(_Strict):
    overnight_gap_alert: float = 0.08


class MarketStress(_Strict):
    block_entries_in_regimes: list[str] = ["MARKET_STRESS"]
    exposure_cap_in_regimes: dict[str, float] = {}


class RiskLimits(_Strict):
    position: PositionLimits = PositionLimits()
    portfolio: PortfolioLimits = PortfolioLimits()
    liquidity: LiquidityLimits = LiquidityLimits()
    loss_limits: LossLimits = LossLimits()
    gap_risk: GapRisk = GapRisk()
    market_stress: MarketStress = MarketStress()


# --------------------------------------------------------------------------- strategies
class StrategyEntry(_Strict):
    enabled: bool = True
    family: str
    params: dict[str, Any] = {}
    grid: dict[str, list[Any]] = {}


class RegimeCfg(_Strict):
    benchmark: str = "NIFTY50"
    trend_window: int = 200
    slope_window: int = 50
    vol_window: int = 21
    vol_high_pct: float = 0.8
    vol_low_pct: float = 0.2
    stress_drawdown: float = 0.15
    min_history: int = 252


class PortfolioConstructionCfg(_Strict):
    method: str = "inverse_volatility"
    lookback_days: int = 126
    max_strategy_weight: float = 0.4


class StrategiesCfg(_Strict):
    strategies: dict[str, StrategyEntry]
    regime: RegimeCfg = RegimeCfg()
    regime_gating: dict[str, dict[str, list[str]]] = {}
    portfolio_construction: PortfolioConstructionCfg = PortfolioConstructionCfg()


# --------------------------------------------------------------------------- costs
class CostsCfg(BaseModel):
    """Transaction-cost schedule. Validated structurally in bql.costs."""

    model_config = ConfigDict(extra="allow")
    currency: str = "INR"
    product: str = "delivery"


class AppConfig(BaseModel):
    settings: Settings
    risk: RiskLimits
    strategies: StrategiesCfg
    costs: dict[str, Any]
    root: Path = PROJECT_ROOT

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def path(self, name: str) -> Path:
        return self.settings.paths.resolve(name, self.root)

    def config_hash(self) -> str:
        payload = {
            "settings": self.settings.model_dump(mode="json"),
            "risk": self.risk.model_dump(mode="json"),
            "strategies": self.strategies.model_dump(mode="json"),
            "costs": self.costs,
        }
        blob = json.dumps(payload, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:16]


def _read_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def _validate_costs(costs: dict[str, Any]) -> dict[str, Any]:
    CostsCfg.model_validate(costs)
    required = ["brokerage", "stt", "exchange_txn", "sebi_turnover_fee", "stamp_duty", "gst", "execution"]
    missing = [k for k in required if k not in costs]
    if missing:
        raise ValueError(f"transaction_costs.yaml missing sections: {missing}")
    if costs.get("product") != "delivery":
        raise ValueError("Only delivery-based cash equity simulation is supported")
    for key in ["brokerage", "stt", "sebi_turnover_fee", "stamp_duty", "gst"]:
        for entry in costs[key]:
            date.fromisoformat(str(entry["effective_from"]))
    return costs


def load_config(config_dir: Path | str | None = None, root: Path | str | None = None) -> AppConfig:
    """Load and validate all configuration files.

    `BQL_CONFIG_DIR` environment variable may point to an alternative config directory.
    """
    cdir = Path(config_dir or os.environ.get("BQL_CONFIG_DIR", CONFIG_DIR))
    root_path = Path(root) if root else PROJECT_ROOT
    settings = Settings.model_validate(_read_yaml(cdir / "settings.yaml"))
    v = settings.validation
    if v.train_fraction + v.validation_fraction >= 1.0:
        raise ValueError("train_fraction + validation_fraction must leave a non-empty test period")
    risk = RiskLimits.model_validate(_read_yaml(cdir / "risk_limits.yaml"))
    strategies = StrategiesCfg.model_validate(_read_yaml(cdir / "strategies.yaml"))
    costs = _validate_costs(_read_yaml(cdir / "transaction_costs.yaml"))
    return AppConfig(settings=settings, risk=risk, strategies=strategies, costs=costs, root=root_path)
