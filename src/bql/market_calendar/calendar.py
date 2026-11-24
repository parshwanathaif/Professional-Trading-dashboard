"""Exchange trading calendar (Asia/Kolkata).

Sources, in order of precedence:
1. An imported holiday list (official NSE/BSE circulars, see data/reference/README).
2. Weekend rule (exchanges are closed on Saturdays and Sundays).

Special sessions (e.g. Muhurat trading on Diwali, which can fall on a weekend or holiday)
are supported through `special_sessions`. When no holiday list has been imported for a
year, the calendar reports `holiday_list_verified(year) == False` and the daily workflow
falls back to "trading day = a day on which validated benchmark data exist".
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


class TradingCalendar:
    def __init__(
        self,
        holidays: Iterable[date | str | pd.Timestamp] = (),
        special_sessions: Iterable[date | str | pd.Timestamp] = (),
        verified_years: Iterable[int] = (),
        exchange: str = "NSE",
    ):
        self.exchange = exchange
        self.holidays = {pd.Timestamp(d).normalize() for d in holidays}
        self.special = {pd.Timestamp(d).normalize() for d in special_sessions}
        self.verified_years = set(verified_years) | {h.year for h in self.holidays}

    @classmethod
    def from_store(cls, store, exchange: str = "NSE") -> TradingCalendar:
        try:
            h = store.table("holidays", "exchange = ?", [exchange])
        except Exception:
            h = pd.DataFrame(columns=["date", "description", "verified"])
        special = h[h["description"].fillna("").str.contains("special session", case=False)]["date"]
        regular = h[~h["date"].isin(special)]["date"]
        return cls(regular, special, exchange=exchange)

    def holiday_list_verified(self, year: int) -> bool:
        return year in self.verified_years

    def is_trading_day(self, d) -> bool:
        ts = pd.Timestamp(d).normalize()
        if ts in self.special:
            return True
        return ts.weekday() < 5 and ts not in self.holidays

    def trading_days(self, start, end) -> pd.DatetimeIndex:
        rng = pd.date_range(pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize(), freq="D")
        return pd.DatetimeIndex([d for d in rng if self.is_trading_day(d)])

    def next_trading_day(self, d) -> pd.Timestamp:
        ts = pd.Timestamp(d).normalize() + pd.Timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(ts):
                return ts
            ts += pd.Timedelta(days=1)
        raise RuntimeError("no trading day found within 30 days")

    def previous_trading_day(self, d) -> pd.Timestamp:
        ts = pd.Timestamp(d).normalize() - pd.Timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(ts):
                return ts
            ts -= pd.Timedelta(days=1)
        raise RuntimeError("no trading day found within 30 days")

    @staticmethod
    def now_ist() -> datetime:
        return datetime.now(IST)

    def session_closed(self, at: datetime | None = None) -> bool:
        """True when the regular session for `at`'s date has ended (or it is not a trading day)."""
        at = at or self.now_ist()
        if at.tzinfo is None:
            at = at.replace(tzinfo=IST)
        at = at.astimezone(IST)
        if not self.is_trading_day(at.date()):
            return True
        return at.time() >= MARKET_CLOSE
