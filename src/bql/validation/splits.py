"""Chronological data splits and walk-forward folds (no shuffling, ever)."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class Segment:
    name: str
    start: pd.Timestamp
    end: pd.Timestamp

    def as_dict(self) -> dict:
        return {"name": self.name, "start": str(self.start.date()), "end": str(self.end.date())}


def chronological_split(dates: pd.DatetimeIndex, train_frac: float, val_frac: float,
                        warmup_days: int = 0) -> dict[str, Segment]:
    """Split [warmup..end] into train / validation / untouched test, in time order."""
    usable = dates[warmup_days:]
    n = len(usable)
    if n < 300:
        raise ValueError("not enough history for a train/validation/test split")
    i1 = int(n * train_frac)
    i2 = int(n * (train_frac + val_frac))
    return {
        "train": Segment("train", usable[0], usable[i1 - 1]),
        "validation": Segment("validation", usable[i1], usable[i2 - 1]),
        "test": Segment("test", usable[i2], usable[-1]),
    }


def walk_forward_folds(dates: pd.DatetimeIndex, start: pd.Timestamp, end: pd.Timestamp, train_years: int,
                       test_years: int, step_years: int) -> list[dict]:
    """Rolling folds fully inside [start, end]; each test window follows its train window."""
    folds = []
    cur = pd.Timestamp(start)
    while True:
        tr_end = cur + pd.DateOffset(years=train_years) - pd.Timedelta(days=1)
        te_start = tr_end + pd.Timedelta(days=1)
        te_end = te_start + pd.DateOffset(years=test_years) - pd.Timedelta(days=1)
        if te_start > end:
            break
        te_end = min(te_end, pd.Timestamp(end))
        d_tr = dates[(dates >= cur) & (dates <= tr_end)]
        d_te = dates[(dates >= te_start) & (dates <= te_end)]
        if len(d_tr) < 200 or len(d_te) < 60:
            break
        folds.append({"fold": len(folds) + 1, "train_start": d_tr[0], "train_end": d_tr[-1],
                      "test_start": d_te[0], "test_end": d_te[-1]})
        cur = cur + pd.DateOffset(years=step_years)
    return folds
