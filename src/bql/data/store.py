"""DuckDB-backed historical market database with provenance tracking.

Design rules
------------
* Raw (unadjusted) prices are stored exactly as imported. Adjusted series are derived on
  read from the corporate-actions table, so adjustments are reproducible and auditable.
* Every import gets a provenance record (source, file hash, row counts, IST timestamp,
  licence note, synthetic flag). Every stored row carries its `import_id`.
* Exact duplicate rows are idempotent. Conflicting duplicates (same key, different values)
  are never silently resolved: they are written to `quarantine` and excluded.
* Missing prices are never filled.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pandas as pd

from ..logging_setup import get_logger, log_event, now_ist
from .providers import ProviderResult
from .schema import DATASETS, PRIMARY_KEYS

log = get_logger("data.store")

_DDL = {
    "prices": """CREATE TABLE IF NOT EXISTS prices (
        date DATE, symbol VARCHAR, exchange VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE,
        close DOUBLE, volume DOUBLE, value DOUBLE, series VARCHAR, import_id VARCHAR,
        PRIMARY KEY (date, symbol, exchange))""",
    "securities": """CREATE TABLE IF NOT EXISTS securities (
        symbol VARCHAR, exchange VARCHAR, isin VARCHAR, name VARCHAR, sector VARCHAR,
        industry VARCHAR, listing_date DATE, delisting_date DATE, delisting_reason VARCHAR,
        import_id VARCHAR, PRIMARY KEY (symbol, exchange))""",
    "corporate_actions": """CREATE TABLE IF NOT EXISTS corporate_actions (
        symbol VARCHAR, exchange VARCHAR, ex_date DATE, action_type VARCHAR, ratio_from DOUBLE,
        ratio_to DOUBLE, cash_amount DOUBLE, rights_price DOUBLE, new_symbol VARCHAR,
        adjustment_factor DOUBLE, notes VARCHAR, import_id VARCHAR,
        PRIMARY KEY (symbol, exchange, ex_date, action_type))""",
    "index_membership": """CREATE TABLE IF NOT EXISTS index_membership (
        index_name VARCHAR, symbol VARCHAR, start_date DATE, end_date DATE, import_id VARCHAR,
        PRIMARY KEY (index_name, symbol, start_date))""",
    "benchmarks": """CREATE TABLE IF NOT EXISTS benchmarks (
        date DATE, index_name VARCHAR, close DOUBLE, open DOUBLE, high DOUBLE, low DOUBLE,
        import_id VARCHAR, PRIMARY KEY (date, index_name))""",
    "holidays": """CREATE TABLE IF NOT EXISTS holidays (
        date DATE, exchange VARCHAR, description VARCHAR, source VARCHAR, verified BOOLEAN,
        import_id VARCHAR, PRIMARY KEY (date, exchange))""",
    "price_bands": """CREATE TABLE IF NOT EXISTS price_bands (
        symbol VARCHAR, start_date DATE, band_pct DOUBLE, end_date DATE, exchange VARCHAR,
        import_id VARCHAR, PRIMARY KEY (symbol, exchange, start_date))""",
    "provenance": """CREATE TABLE IF NOT EXISTS provenance (
        import_id VARCHAR PRIMARY KEY, dataset VARCHAR, source_name VARCHAR, source_uri VARCHAR,
        file_sha256 VARCHAR, license_note VARCHAR, is_synthetic BOOLEAN, rows_in INTEGER,
        rows_inserted INTEGER, rows_exact_duplicate INTEGER, rows_quarantined INTEGER,
        imported_at_ist VARCHAR, notes VARCHAR)""",
    "quarantine": """CREATE TABLE IF NOT EXISTS quarantine (
        import_id VARCHAR, dataset VARCHAR, reason VARCHAR, record_json VARCHAR,
        quarantined_at_ist VARCHAR)""",
}


class MarketStore:
    def __init__(self, path: Path | str, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as con:
                for ddl in _DDL.values():
                    con.execute(ddl)

    @contextmanager
    def connect(self):
        con = duckdb.connect(str(self.path), read_only=self.read_only)
        try:
            yield con
        finally:
            con.close()

    # ------------------------------------------------------------------ import
    def ingest(self, result: ProviderResult) -> dict:
        if self.read_only:
            raise PermissionError("store opened read-only")
        dataset = result.dataset
        df = result.frame.copy()
        keys = PRIMARY_KEYS[dataset]
        import_id = f"{dataset}-{now_ist():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
        cols = list(DATASETS[dataset][0]) + list(DATASETS[dataset][1].keys())
        rows_in = len(df)

        # 1) within-file duplicates
        exact_mask = df.duplicated(subset=cols, keep="first")
        n_exact = int(exact_mask.sum())
        df = df[~exact_mask]
        conflict_mask = df.duplicated(subset=keys, keep=False)
        quarantined = df[conflict_mask]
        df = df[~conflict_mask]

        with self.connect() as con:
            # 2) conflicts with rows already stored
            sql = f"SELECT {', '.join(cols)} FROM {dataset}"
            params: list = []
            if "date" in keys and len(df):
                # only rows that can collide with the incoming batch (keeps multi-million-row imports fast)
                sql += " WHERE date BETWEEN ? AND ?"
                params = [pd.Timestamp(df["date"].min()).date(), pd.Timestamp(df["date"].max()).date()]
            existing = con.execute(sql, params).df()
            if len(existing) and len(df):
                for c in keys:
                    if pd.api.types.is_datetime64_any_dtype(df[c]):
                        existing[c] = pd.to_datetime(existing[c])
                merged = df.merge(existing, on=keys, how="left", suffixes=("", "__old"), indicator=True)
                in_db = (merged["_merge"] == "both").to_numpy()
                value_cols = [c for c in cols if c not in keys]
                same = pd.Series(True, index=merged.index)
                for c in value_cols:
                    a, b = merged[c], merged[f"{c}__old"]
                    eq = (a == b) | (a.isna() & b.isna())
                    if pd.api.types.is_float_dtype(a) and pd.api.types.is_float_dtype(b):
                        eq = eq | ((a - b).abs() <= 1e-9 * b.abs().clip(lower=1.0))
                    same &= eq.fillna(False).astype(bool)
                same_arr = same.to_numpy()
                n_exact += int((in_db & same_arr).sum())
                conflict_db = in_db & ~same_arr
                quarantined = pd.concat([quarantined, df[conflict_db]])
                df = df[~in_db]

            if len(quarantined):
                qrows = pd.DataFrame(
                    {
                        "import_id": import_id,
                        "dataset": dataset,
                        "reason": "conflicting duplicate key",
                        "record_json": [json.dumps(r, default=str) for r in quarantined.to_dict("records")],
                        "quarantined_at_ist": now_ist().isoformat(timespec="seconds"),
                    }
                )
                con.register("q_df", qrows)
                con.execute("INSERT INTO quarantine BY NAME SELECT * FROM q_df")
                con.unregister("q_df")

            if len(df):
                ins = df[cols].copy()
                ins["import_id"] = import_id
                con.register("ins_df", ins)
                con.execute(f"INSERT INTO {dataset} BY NAME SELECT * FROM ins_df")
                con.unregister("ins_df")

            prov = {
                "import_id": import_id,
                "dataset": dataset,
                "source_name": result.source_name,
                "source_uri": result.source_uri,
                "file_sha256": result.file_sha256,
                "license_note": result.license_note,
                "is_synthetic": bool(result.is_synthetic),
                "rows_in": rows_in,
                "rows_inserted": int(len(df)),
                "rows_exact_duplicate": n_exact,
                "rows_quarantined": int(len(quarantined)),
                "imported_at_ist": now_ist().isoformat(timespec="seconds"),
                "notes": "; ".join(result.notes),
            }
            con.execute(
                "INSERT INTO provenance VALUES (" + ",".join(["?"] * len(prov)) + ")", list(prov.values())
            )
        log_event(log, "import complete", **prov)
        return prov

    def replace_derived(self, result: ProviderResult, where: str, params: list) -> dict:
        """Replace rows that are DERIVED deterministically from retained raw files (e.g. exchange-implied
        corporate-action factors, derived delisting dates). Raw market data are never replaced."""
        if self.read_only:
            raise PermissionError("store opened read-only")
        with self.connect() as con:
            n = con.execute(f"SELECT COUNT(*) FROM {result.dataset} WHERE {where}", params).fetchone()[0]
            con.execute(f"DELETE FROM {result.dataset} WHERE {where}", params)
        result.notes.append(f"replaced {n} previously derived rows ({where})")
        return self.ingest(result)

    # ------------------------------------------------------------------ reads
    def table(self, name: str, where: str = "", params: list | None = None) -> pd.DataFrame:
        if name not in _DDL:
            raise ValueError(f"unknown table {name}")
        sql = f"SELECT * FROM {name}" + (f" WHERE {where}" if where else "")
        with self.connect() as con:
            df = con.execute(sql, params or []).df()
        for c in df.columns:
            if c.endswith("date") or c == "date":
                df[c] = pd.to_datetime(df[c])
        return df

    def prices(self, exchange: str | None = "NSE", start=None, end=None, symbols: list[str] | None = None) -> pd.DataFrame:
        clauses, params = [], []
        if exchange:
            clauses.append("exchange = ?")
            params.append(exchange)
        if start is not None:
            clauses.append("date >= ?")
            params.append(pd.Timestamp(start).date())
        if end is not None:
            clauses.append("date <= ?")
            params.append(pd.Timestamp(end).date())
        if symbols is not None:
            clauses.append("symbol IN (SELECT UNNEST(?))")
            params.append(list(symbols))
        df = self.table("prices", " AND ".join(clauses), params)
        return df.sort_values(["symbol", "date"]).reset_index(drop=True)

    def liquid_symbols(self, min_value_inr: float, exchange: str = "NSE", window: int = 20) -> list[str]:
        """Symbols whose rolling median traded value EVER reached the floor.

        Pre-filtering to these symbols cannot change any research result: a symbol that never meets
        the causal liquidity floor can never enter the universe on any date.
        """
        sql = f"""
            WITH m AS (
              SELECT symbol, MEDIAN(value) OVER (PARTITION BY symbol ORDER BY date
                       ROWS BETWEEN {int(window) - 1} PRECEDING AND CURRENT ROW) AS med
              FROM prices WHERE exchange = ?)
            SELECT symbol FROM m GROUP BY symbol HAVING MAX(med) >= ?"""
        with self.connect() as con:
            return [r[0] for r in con.execute(sql, [exchange, float(min_value_inr)]).fetchall()]

    def equity_symbols(self, isin_prefixes: list[str], exchange: str = "NSE") -> set[str] | None:
        """Symbols that are company shares by ISIN prefix (INE...). ETFs / mutual-fund units (INF...) and
        partly-paid shares (IN9...) are not. Returns None when the securities master has no ISINs."""
        with self.connect() as con:
            rows = con.execute("SELECT symbol, isin FROM securities WHERE exchange = ?", [exchange]).fetchall()
            changes = con.execute("SELECT symbol, new_symbol FROM corporate_actions WHERE exchange = ? AND "
                                  "action_type = 'symbol_change' AND new_symbol IS NOT NULL", [exchange]).fetchall()
        if not rows or all(r[1] is None for r in rows):
            return None
        keep = {sym for sym, isin in rows if isin and any(str(isin).startswith(p) for p in isin_prefixes)}
        # price history uses the symbol traded at the time: include old symbols of kept securities
        mapping = dict(changes)
        for old in mapping:
            cur, seen = old, set()
            while cur in mapping and cur not in seen:
                seen.add(cur)
                cur = mapping[cur]
            if cur in keep:
                keep.add(old)
        return keep

    def source_names(self, dataset: str) -> list[str]:
        with self.connect() as con:
            return [r[0] for r in con.execute("SELECT DISTINCT source_name FROM provenance WHERE dataset = ?",
                                              [dataset]).fetchall()]

    def benchmark(self, index_name: str) -> pd.DataFrame:
        df = self.table("benchmarks", "index_name = ?", [index_name])
        return df.sort_values("date").reset_index(drop=True)

    def any_synthetic(self) -> bool:
        with self.connect() as con:
            r = con.execute("SELECT COUNT(*) FROM provenance WHERE is_synthetic").fetchone()
        return bool(r and r[0])

    def counts(self) -> dict[str, int]:
        out = {}
        with self.connect() as con:
            for t in _DDL:
                out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return out

    def last_price_date(self, exchange: str = "NSE") -> pd.Timestamp | None:
        with self.connect() as con:
            r = con.execute("SELECT MAX(date) FROM prices WHERE exchange = ?", [exchange]).fetchone()
        return pd.Timestamp(r[0]) if r and r[0] is not None else None
