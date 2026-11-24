"""Canonical schemas for every dataset the platform imports.

CSV/Parquet files supplied by a user or by an authorised data provider must map to these
columns. Column names are case-insensitive on import; extra columns are ignored.
"""

from __future__ import annotations

PRICES_REQUIRED = ["date", "symbol", "open", "high", "low", "close", "volume"]
PRICES_OPTIONAL = {"exchange": "NSE", "value": None, "series": "EQ"}

SECURITIES_REQUIRED = ["symbol"]
SECURITIES_OPTIONAL = {
    "exchange": "NSE",
    "isin": None,
    "name": None,
    "sector": "UNKNOWN",
    "industry": None,
    "listing_date": None,
    "delisting_date": None,
    "delisting_reason": None,
}

CORP_ACTIONS_REQUIRED = ["symbol", "ex_date", "action_type"]
CORP_ACTIONS_OPTIONAL = {
    "exchange": "NSE",
    "ratio_from": None,      # split: old shares; bonus: shares held; rights: shares held
    "ratio_to": None,        # split: new shares; bonus: bonus shares;  rights: rights shares
    "cash_amount": None,     # dividend per share (INR, unadjusted)
    "rights_price": None,
    "new_symbol": None,      # symbol_change
    "adjustment_factor": None,  # explicit price factor (>1 lowers pre-event prices), e.g. demerger
    "notes": None,
}
CORP_ACTION_TYPES = {"split", "bonus", "dividend", "rights", "symbol_change", "merger", "demerger", "consolidation"}

INDEX_MEMBERSHIP_REQUIRED = ["index_name", "symbol", "start_date"]
INDEX_MEMBERSHIP_OPTIONAL = {"end_date": None}

BENCHMARK_REQUIRED = ["date", "index_name", "close"]
BENCHMARK_OPTIONAL = {"open": None, "high": None, "low": None}

HOLIDAYS_REQUIRED = ["date"]
HOLIDAYS_OPTIONAL = {"exchange": "NSE", "description": None, "source": None, "verified": False}

PRICE_BANDS_REQUIRED = ["symbol", "start_date", "band_pct"]
PRICE_BANDS_OPTIONAL = {"end_date": None, "exchange": "NSE"}

DATASETS = {
    "prices": (PRICES_REQUIRED, PRICES_OPTIONAL),
    "securities": (SECURITIES_REQUIRED, SECURITIES_OPTIONAL),
    "corporate_actions": (CORP_ACTIONS_REQUIRED, CORP_ACTIONS_OPTIONAL),
    "index_membership": (INDEX_MEMBERSHIP_REQUIRED, INDEX_MEMBERSHIP_OPTIONAL),
    "benchmarks": (BENCHMARK_REQUIRED, BENCHMARK_OPTIONAL),
    "holidays": (HOLIDAYS_REQUIRED, HOLIDAYS_OPTIONAL),
    "price_bands": (PRICE_BANDS_REQUIRED, PRICE_BANDS_OPTIONAL),
}

DATE_COLUMNS = {
    "prices": ["date"],
    "securities": ["listing_date", "delisting_date"],
    "corporate_actions": ["ex_date"],
    "index_membership": ["start_date", "end_date"],
    "benchmarks": ["date"],
    "holidays": ["date"],
    "price_bands": ["start_date", "end_date"],
}

PRIMARY_KEYS = {
    "prices": ["date", "symbol", "exchange"],
    "securities": ["symbol", "exchange"],
    "corporate_actions": ["symbol", "exchange", "ex_date", "action_type"],
    "index_membership": ["index_name", "symbol", "start_date"],
    "benchmarks": ["date", "index_name"],
    "holidays": ["date", "exchange"],
    "price_bands": ["symbol", "exchange", "start_date"],
}
