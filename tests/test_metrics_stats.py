import numpy as np
import pandas as pd
import pytest

from bql.backtesting.metrics import cagr, max_drawdown, recovery_days, sharpe, trade_stats
from bql.portfolio import combine_strategies
from bql.validation import chronological_split, walk_forward_folds
from bql.validation.stats import (
    bootstrap_sharpe,
    deflated_sharpe_ratio,
    probabilistic_sharpe_ratio,
    probability_of_backtest_overfitting,
)


def test_max_drawdown_and_recovery():
    e = pd.Series([100, 120, 90, 60, 130, 140.0])
    assert max_drawdown(e) == pytest.approx(-0.5)
    rec, ok = recovery_days(e)
    assert ok and rec == 1


def test_cagr_two_years():
    e = pd.Series(np.linspace(100, 121, 505))
    assert cagr(e) == pytest.approx(0.1, abs=0.002)


def test_sharpe_nan_for_constant():
    assert np.isnan(sharpe(pd.Series([0.0] * 50)))


def test_trade_stats():
    t = pd.DataFrame({"net_pnl": [100, -50, -50, 200], "return_pct": [0.1, -0.05, -0.05, 0.2], "holding_days": [5] * 4})
    s = trade_stats(t)
    assert s["win_rate"] == 0.5 and s["profit_factor"] == pytest.approx(3.0) and s["max_consecutive_losses"] == 2


def test_psr_and_dsr_behaviour():
    rng = np.random.default_rng(0)
    good = 0.001 + 0.01 * rng.standard_normal(1500)
    noise = 0.01 * rng.standard_normal(1500)
    assert probabilistic_sharpe_ratio(good) > 0.95
    assert probabilistic_sharpe_ratio(noise) < 0.95
    trials = [np.mean(x) / np.std(x) for x in 0.01 * rng.standard_normal((100, 1500))]
    dsr_good, sr0 = deflated_sharpe_ratio(good, trials)
    assert sr0 > 0 and dsr_good < probabilistic_sharpe_ratio(good) + 1e-12


def test_pbo_detects_overfit_noise_vs_true_skill():
    rng = np.random.default_rng(1)
    noise = pd.DataFrame(0.01 * rng.standard_normal((1200, 20)))
    p_noise = probability_of_backtest_overfitting(noise, 8)["pbo"]
    skill = noise.copy()
    skill[0] = skill[0] + 0.003
    p_skill = probability_of_backtest_overfitting(skill, 8)["pbo"]
    assert 0.2 < p_noise < 0.8
    assert p_skill < 0.1


def test_bootstrap_ci_contains_point_estimate():
    r = 0.0005 + 0.01 * np.random.default_rng(2).standard_normal(1000)
    b = bootstrap_sharpe(r, 200, 20, 0)
    point = r.mean() / r.std(ddof=1) * np.sqrt(252)
    assert b["ci_low"] < point < b["ci_high"]


def test_chronological_split_and_walk_forward_no_overlap():
    d = pd.bdate_range("2010-01-01", "2020-12-31")
    sp = chronological_split(d, 0.6, 0.2, warmup_days=100)
    assert sp["train"].end < sp["validation"].start <= sp["validation"].end < sp["test"].start
    folds = walk_forward_folds(d, sp["train"].start, sp["validation"].end, 3, 1, 1)
    assert folds
    for f in folds:
        assert f["train_end"] < f["test_start"] and f["test_end"] <= sp["validation"].end


def test_combine_strategies_weights_sum_to_one():
    r = pd.DataFrame(0.01 * np.random.default_rng(3).standard_normal((300, 3)),
                     index=pd.bdate_range("2020-01-01", periods=300), columns=list("abc"))
    for m in ("equal", "inverse_volatility", "risk_parity"):
        _, w = combine_strategies(r, m, 60, 0.5)
        assert np.allclose(w.sum(axis=1), 1.0)
        assert (w.to_numpy() <= 0.5 + 1e-9).all()
