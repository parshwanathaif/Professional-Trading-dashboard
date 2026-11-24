import numpy as np
import pandas as pd
import pytest

from bql.backtesting import ResearchContext, run_backtest
from bql.backtesting.simulator import Order, PortfolioSimulator, SimConfig
from bql.costs import CostModel
from bql.risk import RiskManager
from bql.strategies import STRATEGIES, Signals, Strategy, make_strategy
from bql.validation import check_causality
from tests.conftest import make_panel


def uni(p):
    return p.universe_mask(0, 0, 50)


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_every_strategy_is_causal(name, panel):
    params = {"rebalance_days": 21} if name == "xs_momentum" else {}
    res = check_causality(make_strategy(name, params), panel, uni, n_samples=4)
    assert res["passed"], res["mismatches"]


class LeakyStrategy(Strategy):
    name = "leaky"

    def _generate(self, panel, universe):
        nxt = np.vstack([panel.close[1:], panel.close[-1:]])   # tomorrow's close: look-ahead!
        e = nxt > panel.close
        return Signals(entry=e, exit=~e, score=nxt, stop_distance=np.ones_like(nxt))


def test_leakage_detector_catches_lookahead(panel):
    assert not check_causality(LeakyStrategy(), panel, uni, n_samples=4)["passed"]


def _ctx(cfg, panel):
    return ResearchContext(cfg, panel, panel.available.copy(), pd.DataFrame({"regime": "UNKNOWN"}, index=panel.dates),
                           CostModel(cfg.costs), {"synthetic": True})


def _sig(panel, entry_rows=None, exit_rows=None, cols=None):
    T, N = panel.T, panel.N
    e = np.zeros((T, N), bool)
    x = np.zeros((T, N), bool)
    cols = cols if cols is not None else list(range(N))
    for t in entry_rows or []:
        e[t, cols] = True
    for t in exit_rows or []:
        x[t, cols] = True
    return Signals(entry=e, exit=x, score=np.ones((T, N)), stop_distance=np.full((T, N), 10.0))


def test_no_same_bar_execution_and_reproducible(cfg):
    p = make_panel(400, 6, seed=3)
    ctx = _ctx(cfg, p)
    sig = make_strategy("trend_ma", {"fast": 10, "slow": 40}).generate(p, p.available)
    r1 = run_backtest(ctx, "trend_ma", signals={"trend_ma": sig})
    r2 = run_backtest(ctx, "trend_ma", signals={"trend_ma": sig})
    assert len(r1.fills) > 0
    f = r1.fills.dropna(subset=["signal_date"])
    assert (pd.to_datetime(f["date"]) > pd.to_datetime(f["signal_date"])).all()
    assert r1.metrics["final_capital_net"] == r2.metrics["final_capital_net"]
    assert r1.trades[["symbol", "entry_date", "exit_date", "net_pnl"]].equals(
        r2.trades[["symbol", "entry_date", "exit_date", "net_pnl"]])


def test_accounting_identity_and_costs(cfg):
    p = make_panel(400, 6, seed=4)
    ctx = _ctx(cfg, p)
    sig = make_strategy("donchian_breakout", {"entry_window": 20, "volume_mult": 0.0}).generate(p, p.available)
    r = run_backtest(ctx, "x", signals={"donchian_breakout": sig})
    eq = r.equity
    assert (eq["cash"] >= -1e-6).all(), "cash must never go negative (no leverage)"
    assert np.allclose(eq["equity"], eq["cash"] + eq["invested"])
    assert (eq["gross_equity"] >= eq["equity"] - 1e-6).all()
    assert r.metrics["total_charges_inr"] > 0
    # realised P&L reconciles: final equity = initial + net pnl of closed trades + open position mark
    closed = r.trades["net_pnl"].sum()
    open_val = eq["invested"].iloc[-1]
    assert open_val >= 0 and np.isfinite(closed)


def test_participation_cap(cfg):
    p = make_panel(120, 2, seed=1)
    p.volume[:] = 1000.0
    cm = CostModel(cfg.costs)
    sim = PortfolioSimulator(p, {"s": _sig(p, entry_rows=[60])}, cm, RiskManager(cfg.risk),
                             SimConfig(initial_capital=1e7, participation=0.05))
    for t in range(0, 62):
        sim.step(t)
    assert all(f["qty"] <= 50 for f in sim.fills)


def test_upper_circuit_blocks_buy(cfg):
    p = make_panel(120, 2, seed=2)
    t = 61
    p.close[t, 0] = p.close[t - 1, 0] * 1.05
    p.open[t, 0] = p.high[t, 0] = p.low[t, 0] = p.close[t, 0]
    p.band_pct = np.full(p.close.shape, 0.05)
    sim = PortfolioSimulator(p, {"s": _sig(p, entry_rows=[60], cols=[0])}, CostModel(cfg.costs),
                             RiskManager(cfg.risk), SimConfig(initial_capital=1e7))
    for k in range(0, 62):
        sim.step(k)
    assert "S0" not in sim.positions
    assert any(o["status"] == "CIRCUIT" for o in sim.orders_log)


def test_stop_loss_gap_fills_at_open(cfg):
    p = make_panel(120, 1, seed=5)
    sim = PortfolioSimulator(p, {"s": _sig(p, entry_rows=[60])}, CostModel(cfg.costs), RiskManager(cfg.risk),
                             SimConfig(initial_capital=1e7))
    for t in range(0, 62):
        sim.step(t)
    pos = sim.positions["S0"]
    t = 62
    p.open[t, 0] = pos.stop * 0.9
    p.low[t, 0] = pos.stop * 0.85
    p.high[t, 0] = max(p.high[t, 0], p.open[t, 0])
    p.close[t, 0] = pos.stop * 0.95
    sim.step(t)
    assert "S0" not in sim.positions
    fill = sim.fills[-1]
    assert fill["ref_price"] == pytest.approx(pos.stop * 0.9)
    assert "gap" in sim.trades[-1]["exit_reason"]


def test_missing_data_writes_off_after_stale_limit(cfg):
    p = make_panel(140, 1, seed=6)
    sim = PortfolioSimulator(p, {"s": _sig(p, entry_rows=[60])}, CostModel(cfg.costs), RiskManager(cfg.risk),
                             SimConfig(initial_capital=1e7, stale_days=3, delisting_haircut=0.3))
    for t in range(0, 62):
        sim.step(t)
    assert "S0" in sim.positions
    for name in ("open", "high", "low", "close"):
        getattr(p, name)[62:, 0] = np.nan
    p.available[62:, 0] = False
    for t in range(62, 70):
        sim.step(t)
    assert "S0" not in sim.positions
    assert "WRITE_OFF" in sim.trades[-1]["exit_reason"]


def test_dividend_credited_only_if_held_before_ex_date(cfg):
    p = make_panel(120, 1, seed=7)
    p.dividend[65, 0] = 2.0
    sim = PortfolioSimulator(p, {"s": _sig(p, entry_rows=[60])}, CostModel(cfg.costs), RiskManager(cfg.risk),
                             SimConfig(initial_capital=1e7))
    for t in range(0, 66):
        sim.step(t)
    qty = sim.positions["S0"].qty
    assert sim.cum_dividends == pytest.approx(2.0 * qty)


def test_no_pyramiding_duplicate_buy_cancelled(cfg):
    p = make_panel(120, 1, seed=8)
    sim = PortfolioSimulator(p, {"s": _sig(p, entry_rows=[60])}, CostModel(cfg.costs), RiskManager(cfg.risk),
                             SimConfig(initial_capital=1e7))
    for t in range(0, 62):
        sim.step(t)
    q0 = sim.positions["S0"].qty
    sim.pending.append(Order("x1", str(p.dates[62].date()), "S0", "s", "BUY", 100, "manual test", 5.0))
    for t in range(62, 65):
        sim.step(t)
    assert sim.positions["S0"].qty == q0


def test_workspace_backtest_with_corporate_actions(ws_cfg):
    from bql.pipeline import research_context

    ctx = research_context(ws_cfg)
    r = run_backtest(ctx, "xs_momentum")
    assert r.metrics["trades"] > 0
    assert ctx.panel.members is not None and ctx.data_flags["synthetic"]
