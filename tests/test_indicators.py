import numpy as np
import pytest

from bql.indicators import atr, bollinger, momentum, rolling_max, rsi, shift, sma, true_range


def test_sma_known_values():
    x = np.arange(1, 11, dtype=float).reshape(-1, 1)
    out = sma(x, 3)
    assert np.isnan(out[1, 0])
    assert out[2, 0] == pytest.approx(2.0)
    assert out[-1, 0] == pytest.approx(9.0)


def test_sma_does_not_fill_missing_values_when_too_sparse():
    x = np.array([1, np.nan, np.nan, 4, 5], dtype=float).reshape(-1, 1)
    out = sma(x, 3)
    assert np.isnan(out[3, 0])  # only 1 valid obs in window -> stays NaN


def test_shift_rejects_negative_lag():
    with pytest.raises(ValueError):
        shift(np.ones((5, 1)), -1)


def test_rsi_extremes():
    up = np.arange(1, 50, dtype=float).reshape(-1, 1)
    assert rsi(up, 14)[-1, 0] == pytest.approx(100.0)
    down = up[::-1]
    assert rsi(down, 14)[-1, 0] == pytest.approx(0.0, abs=1e-9)


def test_true_range_and_atr():
    h = np.array([[10.0], [12.0], [11.0]])
    lo = np.array([[9.0], [10.0], [8.0]])
    c = np.array([[9.5], [11.0], [9.0]])
    tr = true_range(h, lo, c)
    assert tr[1, 0] == pytest.approx(2.5)   # max(2, |12-9.5|, |10-9.5|)
    assert tr[2, 0] == pytest.approx(3.0)
    a = atr(h, lo, c, 2)
    assert np.isfinite(a[-1, 0])


def test_bollinger_band_order():
    x = np.cumsum(np.random.default_rng(0).standard_normal((100, 2)), axis=0) + 100
    mid, up, lo = bollinger(x, 20, 2)
    ok = np.isfinite(mid)
    assert (up[ok] >= mid[ok]).all() and (lo[ok] <= mid[ok]).all()


def test_momentum_skip():
    x = np.arange(1, 31, dtype=float).reshape(-1, 1)
    m = momentum(x, 10, 2)
    assert m[-1, 0] == pytest.approx(x[-3, 0] / x[-11, 0] - 1)


def test_rolling_max_causal():
    x = np.array([1, 5, 2, 3, 4], dtype=float).reshape(-1, 1)
    out = shift(rolling_max(x, 2), 1)
    assert out[2, 0] == 5 and out[4, 0] == 3
