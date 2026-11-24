"""Data-provider interface.

Only legally obtained data may be imported. The platform ships with file-based providers
(CSV / Parquet) for offline use. Authorised vendors (exchange data products, licensed data
vendors, broker historical-data APIs used within their terms) can be added by subclassing
`DataProvider`. Credentials must be supplied locally via environment variables - never
committed to Git and never typed into a chat conversation.

This module deliberately contains NO web scraping.
"""

from __future__ import annotations

import hashlib
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .schema import DATASETS, DATE_COLUMNS


class DataImportError(ValueError):
    pass


@dataclass
class ProviderResult:
    dataset: str
    frame: pd.DataFrame
    source_name: str
    source_uri: str
    file_sha256: str | None
    license_note: str
    is_synthetic: bool = False
    notes: list[str] = field(default_factory=list)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def normalise_frame(dataset: str, df: pd.DataFrame) -> pd.DataFrame:
    """Map a raw frame to the canonical schema. Never fills missing prices."""
    if dataset not in DATASETS:
        raise DataImportError(f"Unknown dataset '{dataset}'")
    required, optional = DATASETS[dataset]
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise DataImportError(f"{dataset}: missing required columns {missing}")
    for col, default in optional.items():
        if col not in df.columns:
            df[col] = default
    df = df[required + list(optional.keys())]
    for col in DATE_COLUMNS.get(dataset, []):
        parsed = pd.to_datetime(df[col], errors="coerce", format="mixed", dayfirst=False)
        bad = df[col].notna() & parsed.isna()
        if bad.any():
            raise DataImportError(f"{dataset}: {int(bad.sum())} unparseable values in '{col}'")
        if getattr(parsed.dt, "tz", None) is not None:
            parsed = parsed.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        df[col] = parsed.dt.normalize()
    for col in ("symbol", "new_symbol", "exchange", "index_name", "action_type", "sector"):
        if col in df.columns:
            df[col] = df[col].astype("string").str.strip()
            if col in ("symbol", "new_symbol", "exchange", "index_name"):
                df[col] = df[col].str.upper()
            if col == "action_type":
                df[col] = df[col].str.lower()
    num_cols = {
        "prices": ["open", "high", "low", "close", "volume", "value"],
        "benchmarks": ["open", "high", "low", "close"],
        "corporate_actions": ["ratio_from", "ratio_to", "cash_amount", "rights_price", "adjustment_factor"],
        "price_bands": ["band_pct"],
    }.get(dataset, [])
    for col in num_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")  # invalid -> NaN, caught by validation
    if dataset == "prices" and df["value"].isna().all():
        # Traded value approximation documented in DATA_REQUIREMENTS.md
        df["value"] = df["close"] * df["volume"]
        df.attrs["value_approximated"] = True
    return df


class DataProvider(ABC):
    """Abstract provider. Implementations must return canonical frames + provenance."""

    name: str = "abstract"
    license_note: str = ""

    @abstractmethod
    def fetch(self, dataset: str, **kwargs) -> ProviderResult: ...


class FileProvider(DataProvider):
    """CSV / Parquet import for offline mode."""

    name = "file"

    def __init__(self, license_note: str = "User-supplied file; user attests to lawful licence"):
        self.license_note = license_note

    def fetch(self, dataset: str, path: str | Path | None = None, **kwargs) -> ProviderResult:
        if path is None:
            raise DataImportError("FileProvider requires a path")
        p = Path(path)
        if not p.exists():
            raise DataImportError(f"File not found: {p}")
        suffix = p.suffix.lower()
        if p.stat().st_size == 0:
            raise DataImportError(f"File is empty: {p}")
        if suffix == ".csv":
            raw = pd.read_csv(p)
        elif suffix in (".parquet", ".pq"):
            raw = pd.read_parquet(p)
        else:
            raise DataImportError(f"Unsupported file type {suffix}; use .csv or .parquet")
        frame = normalise_frame(dataset, raw)
        notes = []
        if frame.attrs.get("value_approximated"):
            notes.append("value column absent; approximated as close*volume")
        synthetic = "synthetic" in p.name.lower() or bool(kwargs.get("is_synthetic"))
        return ProviderResult(
            dataset=dataset,
            frame=frame,
            source_name=kwargs.get("source_name", p.name),
            source_uri=str(p.resolve()),
            file_sha256=file_sha256(p),
            license_note=kwargs.get("license_note", self.license_note),
            is_synthetic=synthetic,
            notes=notes,
        )


class AuthorisedVendorProvider(DataProvider):
    """Template for a licensed vendor/API integration.

    Reads credentials from environment variables only (see .env.example). Implement
    `_download` against the vendor's documented, licensed API and respect its rate limits
    and terms. Left unimplemented on purpose: no vendor is licensed in this installation.
    """

    name = "authorised_vendor"
    license_note = "Licensed vendor feed - see vendor agreement"
    credential_env = "BQL_VENDOR_API_KEY"

    def __init__(self) -> None:
        self.api_key = os.environ.get(self.credential_env)

    def fetch(self, dataset: str, **kwargs) -> ProviderResult:
        if not self.api_key:
            raise DataImportError(
                f"{self.credential_env} not set. Configure credentials locally in .env; "
                "never paste them into chat or commit them."
            )
        raise NotImplementedError("Implement against your licensed vendor's documented API.")
