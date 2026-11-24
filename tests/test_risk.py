import numpy as np
import pytest

from bql.backtesting.simulator import PortfolioSimulator, SimConfig
from bql.costs import CostModel
from bql.risk import RiskManager
from bql.strategies import Signals
from tests.conftest import make_panel


def size(rm, **kw):
    base = dict(equity=1e7, cash_available=1e7, invested_value=0.0, sector_value=0.0, n_open=0, price=100.0,
                stop_distance=5.0, adv_value=1e12, exposure_cap=1.0)
    base.update(kw)
    return rm.size_position(**base)


def test_risk_budget_binding(cfg):
    d = size(RiskManager(cfg.risk), stop_distance=20.0)
    # 0.75% of 1cr = 75,000 / 20 = 3,750 shares (weight cap 8% = 8,000 shares)
    assert d.qty == 3750 and d.binding == "risk_budget"


def test_weight_liquidity_cash_sector_caps(cfg):
    rm = RiskManager(cfg.risk)
    assert size(rm, stop_distance=0.5).binding == "max_position_weight"
    assert size(rm, adv_value=1e7).binding == "liquidity_adv"
    d = size(rm, cash_available=300000.0)
    assert d.binding == "cash" or not d.approved
    assert size(rm, sector_value=0.25e7).approved is False
    d = size(rm, invested_value=0.995e7)
    assert d.binding == "exposure_cap" and d.qty == 500         # INR 50k headroom left
    assert size(rm, invested_value=0.999e7).approved is False   # INR 10k < minimum ticket


def test_max_open_positions(cfg):
    d = size(RiskManager(cfg.risk), n_open=cfg.risk.position.max_open_positions)
    assert not d.approved and d.binding == "max_open_positions"


def test_invalid_stop_rejected(cfg):
    assert not size(RiskManager(cfg.risk), stop_distance=float("nan")).approved


def test_kill_switch_triggers_blocks_and_requires_manual_reset(cfg):
    rm = RiskManager(cfg.risk)
    rm.update_after_close("2024-01-01", 1e7)
    ev = rm.update_after_close("2024-01-02", 0.79e7)
    assert any(e.code == "KILL_SWITCH" for e in ev)
    ok, why = rm.entries_allowed("2024-01-03", "s", None)
    assert not ok and "KILL SWITCH" in why
    # recovery alone does not clear it
    rm.update_after_close("2024-01-10", 1.1e7)
    assert rm.state.kill_switch
    with pytest.raises(ValueError):
        rm.reset_kill_switch("", "")
    rm.reset_kill_switch("analyst", "reviewed")
    assert rm.entries_allowed("2024-01-11", "s", None)[0]


def test_research_mode_logs_but_does_not_enforce(cfg):
    rm = RiskManager(cfg.risk, enforce_hard_limits=False)
    rm.update_after_close("2024-01-01", 1e7)
    rm.update_after_close("2024-01-02", 0.7e7)
    assert not rm.state.kill_switch
    assert rm.would_have_triggered and rm.would_have_triggered[0]["code"] == "KILL_SWITCH"


def test_daily_loss_soft_block_auto_clears(cfg):
    rm = RiskManager(cfg.risk)
    rm.update_after_close("2024-01-01", 1e7)
    rm.update_after_close("2024-01-02", 0.965e7)    # -3.5%
    assert not rm.entries_allowed("2024-01-03", "s", None)[0]
    assert rm.entries_allowed("2024-01-08", "s", None)[0] or rm.state.entry_block_reason.startswith("weekly")


def test_consecutive_losses_suspend_strategy(cfg):
    rm = RiskManager(cfg.risk)
    for _i in range(cfg.risk.loss_limits.max_consecutive_losses):
        rm.record_trade_result("2024-01-01", "s", -1.0)
    assert not rm.entries_allowed("2024-01-02", "s", None)[0]
    assert rm.entries_allowed("2024-01-02", "other", None)[0]
    rm.reset_strategy("s", "analyst", "reviewed")
    assert rm.entries_allowed("2024-01-02", "s", None)[0]


def test_market_stress_regime_blocks_entries(cfg):
    rm = RiskManager(cfg.risk)
    assert not rm.entries_allowed("2024-01-02", "s", "MARKET_STRESS")[0]
    assert rm.exposure_cap("MARKET_STRESS", None) == pytest.approx(0.30)
    assert rm.exposure_cap(None, 0.30) == pytest.approx(0.5)   # vol targeting 15%/30%


def test_correlation_limit(cfg):
    rm = RiskManager(cfg.risk)
    x = np.random.default_rng(0).standard_normal(120)
    ok, c = rm.correlation_ok(x, np.column_stack([x, x * 2]))
    assert not ok and c == pytest.approx(1.0)
    ok2, _ = rm.correlation_ok(x, np.random.default_rng(1).standard_normal((120, 2)))
    assert ok2


def test_strategy_cannot_bypass_risk_engine_after_kill(cfg):
    """Even with an entry signal every day, no BUY is created once the kill switch is active."""
    p = make_panel(200, 4, seed=9)
    T, N = p.T, p.N
    sig = Signals(entry=np.ones((T, N), bool), exit=np.zeros((T, N), bool), score=np.ones((T, N)),
                  stop_distance=np.full((T, N), 5.0))
    rm = RiskManager(cfg.risk)
    rm.state.kill_switch, rm.state.kill_reason, rm.state.kill_date = True, "test", "2018-01-01"
    sim = PortfolioSimulator(p, {"s": sig}, CostModel(cfg.costs), rm, SimConfig(initial_capital=1e7))
    for t in range(T):
        sim.step(t)
    assert not sim.fills and all(s["decision"] == "REJECTED" for s in sim.signal_log)
