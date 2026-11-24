"""Look-ahead / future-data leakage test.

For a sample of dates t, signals computed on the panel truncated at t must be IDENTICAL to the
row-t signals computed on the full panel. Any difference proves that the strategy (or the
universe filter) uses information from after t.
"""

from __future__ import annotations

import numpy as np

from ..data.panel import Panel
from ..strategies.base import Strategy


def check_causality(strategy: Strategy, panel: Panel, universe_fn, n_samples: int = 6, seed: int = 0) -> dict:
    full_uni = universe_fn(panel)
    full = strategy.generate(panel, full_uni)
    rng = np.random.default_rng(seed)
    lo = min(300, panel.T - 2)
    ts = sorted(set(rng.integers(lo, panel.T - 1, size=n_samples).tolist()))
    mismatches = []
    for t in ts:
        cut = panel.truncate(panel.dates[t])
        part = strategy.generate(cut, universe_fn(cut))
        for name in ("entry", "exit"):
            a, b = getattr(full, name)[t], getattr(part, name)[t]
            if not np.array_equal(a, b):
                mismatches.append({"date": str(panel.dates[t].date()), "field": name, "n_diff": int((a != b).sum())})
        for name in ("score", "stop_distance"):
            a, b = getattr(full, name)[t], getattr(part, name)[t]
            if not np.allclose(a, b, equal_nan=True, rtol=1e-9, atol=1e-12):
                mismatches.append({"date": str(panel.dates[t].date()), "field": name})
    return {"passed": not mismatches, "dates_tested": [str(panel.dates[t].date()) for t in ts],
            "mismatches": mismatches}
