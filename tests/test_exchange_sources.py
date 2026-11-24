"""Parsers and derivations for official NSE/BSE archive files (offline; fixtures mimic the real formats)."""

import json
import zipfile
from datetime import date

import numpy as np
import pandas as pd
import pytest

from bql.data.exchange_sources import (
    ArchiveClient,
    bse_cm_url,
    build_datasets,
    derive_corporate_actions,
    detect_missing_sessions,
    normalise_index_name,
    nse_cm_url,
    nse_index_url,
    parse_nse_cm,
    parse_nse_index,
    parse_purpose,
    validated_symbol_changes,
)
from bql.data.panel import Panel

OLD_HDR = "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,\n"
NEW_HDR = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,"
           "OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,"
           "ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4\n")


def _zip(path, name, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(name, text)


def _old_row(sym, series, o, h, lo, c, prev, d):
    return f"{sym},{series},{o},{h},{lo},{c},{c},{prev},1000,{c * 1000},{d},10,INE000000001,\n"


def _new_row(sym, series, o, h, lo, c, prev, d):
    return (f"{d},{d},CM,NSE,STK,1,INE000000001,{sym},{series},,,,,NAME,{o},{h},{lo},{c},{c},{prev},,{c},,,"
            f"1000,{c * 1000},10,F1,1,,,,,\n")


def test_url_formats_switch_at_udiff_start():
    assert nse_cm_url(date(2013, 1, 2)).endswith("/historical/EQUITIES/2013/JAN/cm02JAN2013bhav.csv.zip")
    assert nse_cm_url(date(2024, 7, 8)).endswith("/cm/BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv.zip")
    assert nse_index_url(date(2026, 11, 20)).endswith("ind_close_all_20112026.csv")
    assert bse_cm_url(date(2026, 11, 20)).endswith("BhavCopy_BSE_CM_0_0_0_20261120_F_0000.CSV")


def test_parse_both_bhavcopy_formats(tmp_path):
    _zip(tmp_path / "o.zip", "cm.csv", OLD_HDR + _old_row("AAA", "EQ", 10, 11, 9, 10.5, 10, "02-JAN-2013"))
    _zip(tmp_path / "n.zip", "n.csv", NEW_HDR + _new_row("AAA", "EQ", 10, 11, 9, 10.5, 10, "2026-11-20"))
    o = parse_nse_cm(tmp_path / "o.zip", date(2013, 1, 2))
    n = parse_nse_cm(tmp_path / "n.zip", date(2026, 11, 20))
    for df in (o, n):
        r = df.iloc[0]
        assert (r.symbol, r.series, r.close, r.prevclose, r.volume) == ("AAA", "EQ", 10.5, 10, 1000)
        assert not df["date_mismatch"].any()


def test_index_name_aliases_across_renames():
    assert {normalise_index_name(x) for x in ("S&P CNX Nifty", "CNX Nifty", "Nifty 50")} == {"NIFTY50"}
    assert normalise_index_name("CNX Nifty Junior") == normalise_index_name("Nifty Next 50") == "NIFTYNEXT50"
    assert normalise_index_name("India VIX") == "INDIAVIX"


def test_parse_index_drops_bond_indices(tmp_path):
    p = tmp_path / "i.csv"
    p.write_text("Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,"
                 "Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield\n"
                 "Nifty 50,20-11-2026,25368,25808.8,25345.2,25800.60,1,1,1,1,1,1,1\n"
                 "Nifty 10 yr Benchmark G-Sec,20-11-2026,1,1,1,1,-,-,-,-,-,-,-\n")
    df = parse_nse_index(p)
    assert list(df["index_name"]) == ["NIFTY50"] and df["close"].iloc[0] == pytest.approx(25800.6)


def test_parse_purpose_strings():
    assert parse_purpose("FV SPLT FRM RS 2 TO RS 1") == [{"action_type": "split", "ratio_from": 1.0, "ratio_to": 2.0}]
    assert parse_purpose("FV SPLIT FRM RS10 TO RE 1")[0]["ratio_to"] == 10.0
    assert parse_purpose("BONUS 1:2") == [{"action_type": "bonus", "ratio_from": 2.0, "ratio_to": 1.0}]
    d = parse_purpose("AGM/DIV-RS 3.5/ SPL DIV-RS 2")
    assert d == [{"action_type": "dividend", "cash_amount": 5.5}]
    assert parse_purpose("DIVID RS 1.80/- PER SHARE")[0]["cash_amount"] == 1.8
    r = parse_purpose("RIGHTS 7:5 @ PRM RS 8/-")[0]
    assert (r["ratio_to"], r["ratio_from"], r["rights_premium_only"], r["rights_amount"]) == (7, 5, True, 8)
    assert parse_purpose("ANNUAL GENERAL MEETING") == []
    assert parse_purpose("CONSOLIDATION OF SHARES FROM RS 1 TO RS 10")[0]["adjustment_factor"] == pytest.approx(0.1)


def _bc(rows):
    return pd.DataFrame(rows, columns=["symbol", "series", "record_dt", "bc_start", "ex_dt", "purpose"])


def test_split_accepted_only_when_prices_confirm():
    d = pd.bdate_range("2019-09-16", periods=6)
    prices = pd.DataFrame({"date": d, "symbol": "HDFCBANK", "open": [2250, 2240, 2210, 1100, 1105, 1200],
                           "close": [2244.15, 2211.35, 2187.75, 1101.05, 1199.6, 1257.25]})
    bc = _bc([["HDFCBANK", "EQ", pd.NaT, pd.NaT, pd.Timestamp("2019-09-18"), "FV SPLT FRM RS 2 TO RS 1"],
              ["HDFCBANK", "EQ", pd.NaT, pd.NaT, pd.Timestamp("2019-09-17"), "FV SPLT FRM RS 10 TO RS 1"],
              ["HDFCBANK", "EQ", pd.Timestamp("2019-09-20"), pd.NaT, pd.NaT, "DIV-RS 5 PER SH"]])
    out, st = derive_corporate_actions(bc, prices, {}, {})
    split = out[out["action_type"] == "split"]
    assert len(split) == 1 and split["ex_date"].iloc[0] == d[3]          # snapped to the real gap day
    assert st["rejected_no_price_match"] == 1                             # 10:1 'split' has no matching gap
    div = out[out["action_type"] == "dividend"]
    assert div["cash_amount"].iloc[0] == 5 and div["ex_date"].iloc[0] == d[3]   # T+2 era: record - 1 session


def test_same_day_bonus_and_split_verified_on_combined_factor():
    d = pd.bdate_range("2025-06-11", periods=5)
    prices = pd.DataFrame({"date": d, "symbol": "BAJFINANCE", "open": [9300, 9350, 9400, 940, 945],
                           "close": [9320, 9380, 9410, 935, 950]})        # x10 gap on 2025-06-16 (Mon)
    bc = _bc([["BAJFINANCE", "EQ", pd.NaT, pd.NaT, pd.Timestamp("2025-06-16"), "BONUS 4:1"],
              ["BAJFINANCE", "EQ", pd.NaT, pd.NaT, pd.Timestamp("2025-06-16"), "FV SPLT FRM RS 2 TO RE 1"]])
    out, st = derive_corporate_actions(bc, prices, {}, {})
    assert set(out["action_type"]) == {"bonus", "split"} and (out["ex_date"] == d[3]).all()
    assert st["rejected_no_price_match"] == 0


def test_detect_missed_weekend_session():
    d = [pd.Timestamp("2016-10-28"), pd.Timestamp("2016-10-31")]        # Sunday 30-Oct Muhurat session missing
    rows = []
    for i in range(10):
        rows.append({"date": d[0], "symbol": f"S{i}", "close": 100.0, "prevclose": 99.0})
        rows.append({"date": d[1], "symbol": f"S{i}", "close": 102.0, "prevclose": 101.0})   # prev != 100
    missing = detect_missing_sessions(pd.DataFrame(rows))
    assert date(2016, 10, 30) in missing and date(2016, 10, 29) in missing


def test_symbol_change_only_when_data_confirm_handover(tmp_path):
    d1, d2 = pd.bdate_range("2020-01-01", periods=5), pd.bdate_range("2020-01-08", periods=5)
    prices = pd.concat([pd.DataFrame({"date": d1, "symbol": "OLD"}), pd.DataFrame({"date": d2, "symbol": "NEW"}),
                        pd.DataFrame({"date": d1, "symbol": "X1"}), pd.DataFrame({"date": d1, "symbol": "X2"})])
    f = tmp_path / "sc.csv"
    f.write_text("NAME OF THE COMPANY,OLD SYMBOL,NEW SYMBOL,EFFECTIVE DATE\nA,OLD,NEW,08-JAN-2020\n"
                 "B,X1,X2,08-JAN-2020\n")   # X1/X2 overlap in time -> rejected (would merge two companies)
    sc = validated_symbol_changes(prices, f)
    assert list(zip(sc["symbol"], sc["new_symbol"])) == [("OLD", "NEW")]


def test_archive_client_resumes_from_manifest_without_network(tmp_path):
    cli = ArchiveClient(tmp_path)
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "f.csv").write_text("a\n1\n")
    cli.manifest = {"https://example.invalid/f.csv": {"status": 200, "path": "x/f.csv"},
                    "https://example.invalid/missing.csv": {"status": 404}}
    assert cli.fetch("https://example.invalid/f.csv", "x/f.csv") == tmp_path / "x" / "f.csv"
    assert cli.fetch("https://example.invalid/missing.csv", "x/m.csv") is None
    assert cli.stats["cached"] == 1 and cli.stats["missing"] == 1


def test_html_instead_of_data_is_rejected():
    assert not ArchiveClient._looks_valid("https://x/f.zip", b"<!DOCTYPE html><html>")
    assert not ArchiveClient._looks_valid("https://x/f.csv", b"  <html>blocked</html>")
    assert ArchiveClient._looks_valid("https://x/f.zip", b"PK\x03\x04...")


def test_build_datasets_end_to_end(tmp_path):
    cache, days = tmp_path / "cache", pd.bdate_range("2013-01-01", periods=6)
    manifest = {}
    closes = [100, 101, 102, 20.6, 20.8, 21.0]
    prevs = [99, 100, 101, 20.4, None, 20.6]              # day 3: exchange-adjusted for a 1:5 split; day 4 holiday
    for i, d in enumerate(days):
        dd = d.date()
        prev = prevs[i]
        if i == 4:
            manifest[nse_cm_url(dd)] = {"status": 404}    # holiday: no files published
            manifest[nse_index_url(dd)] = {"status": 404}
            continue
        rel = f"nse_cm/{dd:%Y}/{dd:%Y%m%d}.csv.zip"
        _zip(cache / rel, "cm.csv", OLD_HDR + _old_row("AAA", "EQ", closes[i], closes[i] + 1, closes[i] - 1, closes[i],
                                                       prev, d.strftime("%d-%b-%Y").upper())
             + _old_row("AAA", "BE", 1, 1, 1, 1, 1, d.strftime("%d-%b-%Y").upper()))
        manifest[nse_cm_url(dd)] = {"status": 200, "path": rel}
        irel = f"nse_indices/{dd:%Y}/{dd:%Y%m%d}.csv"
        (cache / irel).parent.mkdir(parents=True, exist_ok=True)
        (cache / irel).write_text("Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
                                  f"Closing Index Value\nS&P CNX Nifty,{d:%d-%m-%Y},1,1,1,{5000 + i}\n")
        manifest[nse_index_url(dd)] = {"status": 200, "path": irel}
    (cache / "manifest.json").write_text(json.dumps(manifest))
    bc_text = ("SERIES,SYMBOL,SECURITY,RECORD_DT,BC_STRT_DT,BC_END_DT,EX_DT,ND_STRT_DT,ND_END_DT,PURPOSE\n"
               f"EQ,AAA,AAA LTD,,,,{days[3]:%d/%m/%Y},,,FV SPLT FRM RS 10 TO RS 2\n")
    _zip(cache / "pr" / "2013" / "PR20130104.zip", "Bc040113.csv", bc_text)
    (cache / "pr" / "manifest.json").write_text(json.dumps({"u": {"status": 200, "path": "2013/PR20130104.zip"}}))
    paths = build_datasets(cache, tmp_path / "out")
    pr = pd.read_parquet(paths["prices"])
    assert len(pr) == 5 and (pr["series"] == "EQ").all()            # EQ preferred over BE duplicate
    ca = pd.read_parquet(paths["corporate_actions"])
    assert len(ca) == 1 and ca["action_type"].iloc[0] == "split" and ca["ratio_to"].iloc[0] == pytest.approx(5.0)
    assert pd.Timestamp(ca["ex_date"].iloc[0]) == days[3]
    assert pd.read_parquet(paths["benchmarks"])["index_name"].unique().tolist() == ["NIFTY50"]
    hol = pd.read_parquet(paths["holidays"])
    assert pd.Timestamp(days[4]).date() in set(pd.to_datetime(hol["date"]).dt.date)


def test_liquidity_top_n_universe_is_causal():
    from tests.conftest import make_panel

    p = make_panel(400, 10, seed=1)
    p.value[:] = np.arange(1, 11) * 1e8                     # S9 most liquid
    p.value[300:, 0] = 1e12                                 # S0 becomes most liquid only later
    full = p.universe_mask(0, 0, 50, top_n=3, rank_window=60)
    cut = p.truncate(p.dates[320]).universe_mask(0, 0, 50, top_n=3, rank_window=60)
    assert np.array_equal(full[:321], cut)
    assert full[200, 9] and not full[200, 0] and full[399, 0]
    assert isinstance(p, Panel)


def test_bc_rows_with_unquoted_commas_are_repaired():
    from bql.data.exchange_sources import _read_bc_text

    t = ("SERIES,SYMBOL,SECURITY,RECORD_DT,BC_STRT_DT,BC_END_DT,EX_DT,ND_STRT_DT,ND_END_DT,PURPOSE\n"
         "EQ,ABC,ABC Ltd, India,01/02/2020,,,31/01/2020,,,DIV-RS 2\n"
         "EQ,XYZ,XYZ Ltd,01/02/2020,,,31/01/2020,,,AGM, DIV-RS 3\n")
    df = _read_bc_text(t)
    assert list(df["SECURITY"]) == ["ABC Ltd, India", "XYZ Ltd"]
    assert list(df["EX_DT"]) == ["31/01/2020", "31/01/2020"]
    assert list(df["PURPOSE"]) == ["DIV-RS 2", "AGM, DIV-RS 3"]
