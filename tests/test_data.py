import numpy as np
import pandas as pd
import pytest

from bql.data import DataImportError, FileProvider, MarketStore, validate_prices
from bql.data.corporate_actions import adjust_prices, resolve_symbol_changes
from bql.data.providers import normalise_frame
from bql.market_calendar import TradingCalendar


def _prices(sym="AAA", n=30, start="2024-01-01", price=100.0):
    d = pd.bdate_range(start, periods=n)
    c = price * np.ones(n)
    return pd.DataFrame({"date": d, "symbol": sym, "exchange": "NSE", "open": c, "high": c * 1.01, "low": c * 0.99,
                         "close": c, "volume": 1000.0, "value": c * 1000, "series": "EQ"})


def _bench(n=30, start="2024-01-01"):
    d = pd.bdate_range(start, periods=n)
    return pd.DataFrame({"date": d, "index_name": "IDX", "close": 1000.0})


# ------------------------------------------------------------------ import
def test_missing_required_columns_rejected():
    with pytest.raises(DataImportError):
        normalise_frame("prices", pd.DataFrame({"date": ["2024-01-01"], "symbol": ["A"]}))


def test_value_approximated_and_flagged(tmp_path):
    df = _prices().drop(columns=["value"])
    p = tmp_path / "prices.csv"
    df.to_csv(p, index=False)
    res = FileProvider().fetch("prices", path=p)
    assert res.frame["value"].notna().all()
    assert any("approximated" in n for n in res.notes)


def test_reimport_is_idempotent_and_conflicts_quarantined(tmp_path):
    store = MarketStore(tmp_path / "m.duckdb")
    p = tmp_path / "prices.csv"
    _prices().to_csv(p, index=False)
    a = store.ingest(FileProvider().fetch("prices", path=p))
    b = store.ingest(FileProvider().fetch("prices", path=p))
    assert a["rows_inserted"] == 30 and b["rows_inserted"] == 0 and b["rows_exact_duplicate"] == 30
    changed = _prices()
    changed.loc[0, "close"] = 101.0
    p2 = tmp_path / "prices_rev.csv"
    changed.to_csv(p2, index=False)
    c = store.ingest(FileProvider().fetch("prices", path=p2))
    assert c["rows_quarantined"] == 1 and c["rows_inserted"] == 0
    assert store.counts()["prices"] == 30
    assert float(store.prices()["close"].iloc[0]) == 100.0  # original never silently overwritten
    prov = store.table("provenance")
    assert len(prov) == 3 and prov["file_sha256"].notna().all()


def test_within_file_conflict_quarantined(tmp_path):
    df = pd.concat([_prices(n=3), _prices(n=3).assign(close=99.0)])
    p = tmp_path / "prices.csv"
    df.to_csv(p, index=False)
    store = MarketStore(tmp_path / "m.duckdb")
    r = store.ingest(FileProvider().fetch("prices", path=p))
    assert r["rows_quarantined"] == 6 and r["rows_inserted"] == 0


# ------------------------------------------------------------------ validation
def test_invalid_ohlc_excludes_symbol():
    p = pd.concat([_prices("AAA"), _prices("BBB")])
    p.loc[(p["symbol"] == "BBB") & (p.index == 5), "high"] = 50.0
    rep = validate_prices(p, _bench(), as_of=pd.Timestamp("2025-01-01"))
    assert "BBB" in rep.excluded_symbols and "AAA" not in rep.excluded_symbols
    assert not rep.blocked


def test_missing_benchmark_blocks_run():
    rep = validate_prices(_prices(), None, as_of=pd.Timestamp("2025-01-01"))
    assert rep.blocked


def test_negative_volume_and_future_dates_critical():
    p = _prices()
    p.loc[3, "volume"] = -5
    rep = validate_prices(p, _bench(), as_of=pd.Timestamp("2024-01-20"))
    checks = set(rep.issues["check"])
    assert {"negative_volume", "future_date"} <= checks


def test_missing_bars_detected_and_long_gap_critical():
    p = _prices(n=60)
    b = _bench(n=60)
    short = p.drop(index=[10])
    rep = validate_prices(short, b, as_of=pd.Timestamp("2025-01-01"))
    assert "missing_bars" in set(rep.issues["check"]) and not rep.excluded_symbols
    long = p.drop(index=range(10, 35))
    rep2 = validate_prices(long, b, as_of=pd.Timestamp("2025-01-01"))
    assert "AAA" in rep2.excluded_symbols


def test_weekend_bar_flagged_as_possible_special_session():
    p = _prices(n=5)
    extra = p.iloc[[0]].copy()
    extra["date"] = pd.Timestamp("2024-01-06")  # Saturday
    rep = validate_prices(pd.concat([p, extra]), _bench(n=5), calendar=TradingCalendar(),
                          as_of=pd.Timestamp("2025-01-01"))
    assert (rep.issues["check"] == "non_trading_day_bar").any()


def test_split_move_explained_not_warning():
    p = _prices(n=20)
    p.loc[p.index >= 10, ["open", "high", "low", "close"]] /= 5
    acts = pd.DataFrame({"symbol": ["AAA"], "ex_date": [p["date"].iloc[10]], "action_type": ["split"],
                         "ratio_from": [1], "ratio_to": [5]})
    rep = validate_prices(p, _bench(n=20), actions=acts, as_of=pd.Timestamp("2025-01-01"))
    em = rep.issues[rep.issues["check"] == "extreme_move"]
    assert len(em) == 1 and em["severity"].iloc[0] == "INFO"


# ------------------------------------------------------------------ corporate actions
def _acts(**kw):
    base = {"symbol": "AAA", "ex_date": None, "action_type": None, "ratio_from": None, "ratio_to": None,
            "cash_amount": None, "rights_price": None, "new_symbol": None, "adjustment_factor": None}
    base.update(kw)
    return pd.DataFrame([base])


def test_split_adjustment_makes_series_continuous():
    p = _prices(n=20)
    ex = p["date"].iloc[10]
    p.loc[p["date"] >= ex, ["open", "high", "low", "close"]] /= 5
    adj = adjust_prices(p, _acts(ex_date=ex, action_type="split", ratio_from=1, ratio_to=5))
    assert np.allclose(adj["adj_close"], 20.0)
    assert adj.loc[adj["date"] < ex, "adj_volume"].iloc[0] == pytest.approx(5000.0)


def test_bonus_factor():
    p = _prices(n=10)
    ex = p["date"].iloc[5]
    p.loc[p["date"] >= ex, ["open", "high", "low", "close"]] /= 2   # 1:1 bonus
    adj = adjust_prices(p, _acts(ex_date=ex, action_type="bonus", ratio_from=1, ratio_to=1))
    assert np.allclose(adj["adj_close"], 50.0)


def test_rights_terp_factor():
    p = _prices(n=10)
    ex = p["date"].iloc[5]
    # 1 right for 4 held at 50 -> TERP = (4*100 + 50)/5 = 90
    p.loc[p["date"] >= ex, ["open", "high", "low", "close"]] = 90.0
    adj = adjust_prices(p, _acts(ex_date=ex, action_type="rights", ratio_from=4, ratio_to=1, rights_price=50.0))
    assert np.allclose(adj["adj_close"], 90.0)


def test_dividend_cash_not_price_adjusted():
    p = _prices(n=10)
    ex = p["date"].iloc[4]
    adj = adjust_prices(p, _acts(ex_date=ex, action_type="dividend", cash_amount=2.5))
    assert adj.loc[adj["date"] == ex, "dividend_adj"].iloc[0] == pytest.approx(2.5)
    assert np.allclose(adj["adj_close"], 100.0)


def test_symbol_change_chain_resolved():
    p = pd.concat([_prices("OLD", n=5), _prices("MID", n=5, start="2024-01-08"), _prices("NEW", n=5, start="2024-01-15")])
    acts = pd.DataFrame({"symbol": ["OLD", "MID"], "ex_date": pd.to_datetime(["2024-01-08", "2024-01-15"]),
                         "action_type": ["symbol_change"] * 2, "new_symbol": ["MID", "NEW"]})
    out, mapping = resolve_symbol_changes(p, acts)
    assert set(out["symbol"]) == {"NEW"} and mapping == {"OLD": "NEW", "MID": "NEW"}


def test_calendar_rules():
    cal = TradingCalendar(holidays=["2026-01-26"], special_sessions=["2026-11-08"])
    assert not cal.is_trading_day("2026-01-26")
    assert not cal.is_trading_day("2026-01-24")         # Saturday
    assert cal.is_trading_day("2026-11-08")              # Sunday special (Muhurat) session
    assert cal.next_trading_day("2026-01-23") == pd.Timestamp("2026-01-27")


def test_equity_symbols_excludes_etfs_but_keeps_renamed_stocks(tmp_path):
    store = MarketStore(tmp_path / "m.duckdb")
    sec = pd.DataFrame({"symbol": ["ATGL", "GOLDBEES", "PPSHARE"], "exchange": "NSE",
                        "isin": ["INE399L01023", "INF204KB17I5", "IN9123A01011"]})
    p = tmp_path / "securities.csv"
    sec.to_csv(p, index=False)
    store.ingest(FileProvider().fetch("securities", path=p))
    ca = pd.DataFrame({"symbol": ["ADANIGAS"], "ex_date": ["2021-01-01"], "action_type": ["symbol_change"],
                       "new_symbol": ["ATGL"]})
    p2 = tmp_path / "corporate_actions.csv"
    ca.to_csv(p2, index=False)
    store.ingest(FileProvider().fetch("corporate_actions", path=p2))
    assert store.equity_symbols(["INE"]) == {"ATGL", "ADANIGAS"}
