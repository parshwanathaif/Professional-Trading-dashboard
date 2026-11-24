"""Strategy interface.

A strategy is a pure, causal function of market data:  (panel, params, universe) -> Signals.
It decides WHAT it would like to hold; it never sizes positions, never sees the portfolio and
cannot override risk limits. Sizing, limits, execution and accounting are done by the
independent risk engine and the backtester / paper-trading engine.

Timing contract: values in row t use information available at the CLOSE of day t.
Orders derived from row t are executed no earlier than the next eligible session.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from itertools import product
from typing import Any

import numpy as np

from ..data.panel import Panel
from ..indicators import atr


@dataclass
class Signals:
    entry: np.ndarray                 # bool (T,N): would like to be long after close t
    exit: np.ndarray                  # bool (T,N): exit condition met at close t
    score: np.ndarray                 # float (T,N): ranking of candidates (higher = preferred)
    stop_distance: np.ndarray         # float (T,N): stop distance in price units at t
    max_hold: int | None = None       # strategy time stop (bars)
    info: dict[str, np.ndarray] = field(default_factory=dict)   # indicator values for audit trail
    reasons: dict[str, str] = field(default_factory=dict)      # plain-English entry/exit reasons

    def validate(self, T: int, N: int) -> None:
        for name in ("entry", "exit", "score", "stop_distance"):
            a = getattr(self, name)
            if a.shape != (T, N):
                raise ValueError(f"Signals.{name} has shape {a.shape}, expected {(T, N)}")


class Strategy(ABC):
    name: str = "base"
    family: str = ""
    hypothesis: str = ""
    description: str = ""
    mathematics: str = ""
    default_params: dict[str, Any] = {}

    def __init__(self, params: dict[str, Any] | None = None, atr_window: int = 20, atr_mult: float = 2.5):
        self.params = {**self.default_params, **(params or {})}
        self.atr_window = int(self.params.get("atr_window", atr_window))
        self.atr_mult = atr_mult

    @abstractmethod
    def _generate(self, panel: Panel, universe: np.ndarray) -> Signals: ...

    def generate(self, panel: Panel, universe: np.ndarray | None = None) -> Signals:
        uni = universe if universe is not None else panel.available
        sig = self._generate(panel, uni)
        sig.entry = np.asarray(sig.entry, dtype=bool) & uni & panel.available
        sig.exit = np.asarray(sig.exit, dtype=bool)
        sig.validate(panel.T, panel.N)
        return sig

    def default_stop(self, panel: Panel) -> np.ndarray:
        return self.atr_mult * atr(panel.high, panel.low, panel.close, self.atr_window)

    def describe(self) -> dict[str, str]:
        return {
            "name": self.name, "family": self.family, "hypothesis": self.hypothesis,
            "description": self.description, "mathematics": self.mathematics,
            "params": str(self.params),
        }


def param_grid(grid: dict[str, list[Any]], base: dict[str, Any]) -> list[dict[str, Any]]:
    if not grid:
        return [dict(base)]
    keys = list(grid)
    out = []
    for combo in product(*[grid[k] for k in keys]):
        p = dict(base)
        p.update(dict(zip(keys, combo)))
        out.append(p)
    return out
