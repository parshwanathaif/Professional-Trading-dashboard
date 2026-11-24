import pytest

from bql.costs import CostModel


@pytest.fixture
def cm(cfg):
    return CostModel(cfg.costs)


def test_delivery_stt_both_sides_and_stamp_buy_only(cm):
    v = 1_000_000.0
    b = cm.charges("BUY", v, "2025-06-02")
    s = cm.charges("SELL", v, "2025-06-02")
    assert b.stt == pytest.approx(1000.0)        # 0.1%
    assert s.stt == pytest.approx(1000.0)
    assert b.stamp_duty == pytest.approx(150.0)  # 0.015% buyer
    assert s.stamp_duty == 0.0
    assert b.dp_charge == 0.0 and s.dp_charge > 0


def test_gst_applies_to_brokerage_exchange_sebi_ipft_only(cm):
    b = cm.charges("BUY", 1_000_000.0, "2025-06-02")
    assert b.gst == pytest.approx(0.18 * (b.brokerage + b.exchange_txn + b.sebi_fee + b.ipft))


def test_sebi_fee_ten_rupees_per_crore(cm):
    assert cm.charges("BUY", 1e7, "2024-01-02").sebi_fee == pytest.approx(10.0)


def test_date_effective_exchange_charge_changes(cm):
    before = cm.charges("BUY", 1e7, "2026-02-27").exchange_txn
    after = cm.charges("BUY", 1e7, "2026-03-02").exchange_txn
    assert before == pytest.approx(297.0)
    assert after == pytest.approx(306.9)


def test_historical_stt_rate_before_2013(cm):
    assert cm.charges("SELL", 1e6, "2012-01-02").stt == pytest.approx(1250.0)


def test_cost_stress_multiplier(cm):
    base = cm.charges("BUY", 1e6, "2025-01-02").total
    assert cm.with_stress(cost_multiplier=2).charges("BUY", 1e6, "2025-01-02").total == pytest.approx(2 * base)


def test_execution_price_is_adverse(cm):
    buy, f1 = cm.execution_price("BUY", 100.0, 1000, 1e6, 5e8, 0.02)
    sell, f2 = cm.execution_price("SELL", 100.0, 1000, 1e6, 5e8, 0.02)
    assert buy > 100 > sell and f1 == pytest.approx(f2)


def test_spread_bucket_wider_for_illiquid(cm):
    assert cm.half_spread(1e7) > cm.half_spread(2e9)


def test_round_trip_cost_reasonable(cm):
    rt = cm.round_trip_pct(1e6, "2025-01-02")
    assert 0.002 < rt < 0.004   # ~0.2-0.3% statutory + brokerage for delivery
