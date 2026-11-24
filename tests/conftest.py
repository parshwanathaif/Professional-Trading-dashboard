"""Shared fixtures. All data here is SYNTHETIC and used only for software testing."""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from bql.config import load_config
from bql.data.panel import Panel

ROOT = Path(__file__).resolve().parents[1]


def make_panel(T: int = 600, N: int = 8, seed: int = 0, start: str = "2018-01-01") -> Panel:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=T)
    r = 0.0003 + 0.015 * rng.standard_normal((T, N))
    close = 100 * np.exp(np.cumsum(r, axis=0))
    prev = np.vstack([close[:1], close[:-1]])
    opn = prev * np.exp(0.003 * rng.standard_normal((T, N)))
    high = np.maximum(opn, close) * 1.005
    low = np.minimum(opn, close) * 0.995
    vol = np.full((T, N), 1e6)
    bench = pd.Series(np.exp(np.cumsum(r.mean(axis=1))) * 1000, index=dates)
    return Panel(dates=dates, symbols=[f"S{i}" for i in range(N)], open=opn, high=high, low=low, close=close,
                 raw_close=close.copy(), volume=vol, value=close * vol, dividend=np.zeros((T, N)),
                 available=np.ones((T, N), dtype=bool),
                 sectors=np.array([["A", "B", "C", "D"][i % 4] for i in range(N)], dtype=object), benchmark=bench)


@pytest.fixture
def panel():
    return make_panel()


@pytest.fixture(scope="session")
def cfg():
    return load_config(ROOT / "config", ROOT)


def _make_workspace(base: Path, n_symbols: int = 24, start: str = "2015-01-01", end: str = "2021-06-30") -> Path:
    from bql.data.synthetic import generate
    from bql.pipeline import ensure_dirs, import_file

    ws = base / "ws"
    (ws / "config").mkdir(parents=True)
    for f in (ROOT / "config").glob("*.yaml"):
        shutil.copy(f, ws / "config" / f.name)
    s = yaml.safe_load((ws / "config" / "settings.yaml").read_text())
    s["backtest"]["benchmark"] = "SYNTH_INDEX"
    s["data"]["universe_index"] = "SYNTH30"
    s["data"]["universe_mode"] = "index_membership"
    s["data"]["gaps_are_non_trading"] = False
    s["data"]["critical_row_policy"] = "exclude_symbol"
    s["exchange_data"]["enabled"] = False
    s["data"]["min_median_traded_value_inr"] = 1e6
    s["paper_trading"]["portfolio_name"] = "test_paper"
    s["validation"]["walk_forward"] = {"train_years": 2, "test_years": 1, "step_years": 1}
    s["validation"]["bootstrap_samples"] = 100
    s["validation"]["monte_carlo_paths"] = 100
    s["validation"]["pbo_partitions"] = 6
    (ws / "config" / "settings.yaml").write_text(yaml.safe_dump(s, sort_keys=False))
    st = yaml.safe_load((ws / "config" / "strategies.yaml").read_text())
    st["strategies"]["trend_ma"]["grid"] = {"fast": [20, 50], "slow": [100, 150]}
    (ws / "config" / "strategies.yaml").write_text(yaml.safe_dump(st, sort_keys=False))
    cfg = load_config(ws / "config", ws)
    ensure_dirs(cfg)
    paths = generate(ws / "data" / "synthetic", n_symbols=n_symbols, start=start, end=end, seed=11)
    for ds in ("securities", "corporate_actions", "index_membership", "holidays", "price_bands", "benchmarks",
               "prices"):
        import_file(cfg, ds, paths[ds], "SYNTHETIC test data")
    return ws


@pytest.fixture(scope="session")
def workspace_template(tmp_path_factory):
    return _make_workspace(tmp_path_factory.mktemp("tpl"))


@pytest.fixture
def workspace(workspace_template, tmp_path):
    """Fresh copy of the imported synthetic workspace for tests that mutate state."""
    dst = tmp_path / "ws"
    shutil.copytree(workspace_template, dst)
    return dst


@pytest.fixture
def ws_cfg(workspace):
    return load_config(workspace / "config", workspace)
