"""Official NSE / BSE public-archive data source.

Downloads the end-of-day files the exchanges publish for download on their websites and
converts them to the platform's canonical datasets:

  NSE capital-market bhavcopy (all traded securities, daily)      -> prices (exchange NSE)
  NSE "ind_close_all" daily index file (NIFTY 50, NIFTY 500, ...)  -> benchmarks
  NSE EQUITY_L, delisted list, symbol-change list, index lists     -> securities, symbol changes, sectors
  NSE sec_list (current price bands)                               -> price_bands
  BSE equity bhavcopy (UDiFF format)                               -> prices (exchange BSE, cross-check)
  NSE PR archive "Bc" file (book closures: bonus/split/rights/dividend) -> corporate actions,
                                                                      price-verified ex-dates
  Unadjusted 'previous close' in the bhavcopy                        -> detection of missed sessions
  Weekdays on which NSE published neither file                     -> holidays (derived)

Access rules followed
---------------------
* Only public archive files are requested: no login, no API session cookies, no captcha or
  bot-protection circumvention, an honest User-Agent and a minimum interval between requests.
* Every file is cached locally with its SHA-256; nothing is downloaded twice; 404s are recorded.
* The data are used for personal research under the exchanges' website terms of use and must
  not be redistributed. Provenance records the source URL family and this licence note.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from ..logging_setup import get_logger, log_event, now_ist

log = get_logger("data.exchange")

NSE_ARCHIVE = "https://nsearchives.nseindia.com"
BSE_SITE = "https://www.bseindia.com"
USER_AGENT = "BharatQuantLab-research/0.1 (personal non-commercial research; rate-limited)"
LICENSE_NOTE = ("Official NSE/BSE public website archives (nsearchives.nseindia.com, bseindia.com); "
                "personal research use subject to the exchanges' website terms; not for redistribution")
NSE_SOURCE = "NSE_BHAVCOPY_ARCHIVE"
NSE_UDIFF_START = date(2024, 7, 8)          # first session of the UDiFF bhavcopy format on NSE
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

REFERENCE_FILES = {
    "EQUITY_L.csv": "/content/equities/EQUITY_L.csv",
    "delisted.csv": "/content/equities/delisted.csv",
    "symbolchange.csv": "/content/equities/symbolchange.csv",
    "sec_list.csv": "/content/equities/sec_list.csv",
    "ind_nifty500list.csv": "/content/indices/ind_nifty500list.csv",
    "ind_niftytotalmarket_list.csv": "/content/indices/ind_niftytotalmarket_list.csv",
    "ind_niftymicrocap250_list.csv": "/content/indices/ind_niftymicrocap250_list.csv",
}


class FetchError(RuntimeError):
    pass


# ============================================================================ URLs
def nse_cm_url(d: date) -> str:
    if d >= NSE_UDIFF_START:
        return f"{NSE_ARCHIVE}/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
    mon = MONTHS[d.month - 1]
    return f"{NSE_ARCHIVE}/content/historical/EQUITIES/{d:%Y}/{mon}/cm{d:%d}{mon}{d:%Y}bhav.csv.zip"


def nse_index_url(d: date) -> str:
    return f"{NSE_ARCHIVE}/content/indices/ind_close_all_{d:%d%m%Y}.csv"


def nse_pr_url(d: date) -> str:
    """Daily NSE 'PR' market-report archive; contains Bc*.csv (book closures / corporate actions)."""
    return f"{NSE_ARCHIVE}/archives/equities/bhavcopy/pr/PR{d:%d%m%y}.zip"


def bse_cm_url(d: date) -> str:
    return f"{BSE_SITE}/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{d:%Y%m%d}_F_0000.CSV"


# ============================================================================ client
@dataclass
class ArchiveClient:
    cache_dir: Path
    min_interval: float = 0.35
    timeout: float = 45.0
    retries: int = 3

    def __post_init__(self):
        self.cache_dir = Path(self.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.cache_dir / "manifest.json"
        self.manifest: dict = json.loads(self.manifest_path.read_text()) if self.manifest_path.exists() else {}
        self._last = 0.0
        self._dirty = 0
        self.stats = {"downloaded": 0, "cached": 0, "missing": 0, "bytes": 0}

    def save(self) -> None:
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.manifest, indent=0, sort_keys=True))
        tmp.replace(self.manifest_path)
        self._dirty = 0

    def _wait(self) -> None:
        dt = time.monotonic() - self._last
        if dt < self.min_interval:
            time.sleep(self.min_interval - dt)
        self._last = time.monotonic()

    @staticmethod
    def _looks_valid(url: str, body: bytes) -> bool:
        if url.lower().endswith(".zip"):
            return body[:2] == b"PK"
        head = body[:200].lstrip().lower()
        return not (head.startswith(b"<!doctype html") or head.startswith(b"<html"))

    def fetch(self, url: str, rel_path: str, refresh: bool = False, refresh_missing: bool = False) -> Path | None:
        dest = self.cache_dir / rel_path
        rec = self.manifest.get(url)
        if rec and not refresh:
            if rec["status"] == 200 and dest.exists():
                self.stats["cached"] += 1
                return dest
            if rec["status"] == 404 and not refresh_missing:
                self.stats["missing"] += 1
                return None
        last_err = None
        for attempt in range(self.retries):
            self._wait()
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    body = r.read()
                if not self._looks_valid(url, body):
                    # an HTML page instead of a data file = not published (or access refused): treat as missing
                    self.manifest[url] = {"status": 404, "note": "html instead of data file",
                                          "checked_ist": now_ist().isoformat(timespec="seconds")}
                    self.stats["missing"] += 1
                    self._bump()
                    return None
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(dest.suffix + ".part")
                tmp.write_bytes(body)
                tmp.replace(dest)
                self.manifest[url] = {"status": 200, "path": rel_path, "sha256": hashlib.sha256(body).hexdigest(),
                                      "bytes": len(body), "fetched_ist": now_ist().isoformat(timespec="seconds")}
                self.stats["downloaded"] += 1
                self.stats["bytes"] += len(body)
                self._bump()
                return dest
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    self.manifest[url] = {"status": 404, "checked_ist": now_ist().isoformat(timespec="seconds")}
                    self.stats["missing"] += 1
                    self._bump()
                    return None
                last_err = f"HTTP {e.code}"
                if e.code in (401, 403):
                    raise FetchError(f"{url}: access refused ({e.code}); not retrying or circumventing") from e
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last_err = str(e)
            time.sleep(2.0 * (attempt + 1))
        raise FetchError(f"{url}: failed after {self.retries} attempts ({last_err})")

    def _bump(self) -> None:
        self._dirty += 1
        if self._dirty >= 25:
            self.save()


# ============================================================================ download
def weekdays(start: date, end: date) -> list[date]:
    return [d.date() for d in pd.bdate_range(start, end)]


def download_all(cache_dir: Path, start: date, end: date, bse_start: date | None = None,
                 min_interval: float = 0.35, progress_every: int = 200, refresh_recent_days: int = 7) -> dict:
    """Download (resumably) NSE CM bhavcopies, NSE index files, BSE bhavcopies and reference files."""
    cli = ArchiveClient(cache_dir, min_interval=min_interval)
    today = now_ist().date()
    n = 0
    for d in weekdays(start, end):
        # recently 'missing' files may simply not have been published yet -> re-check them
        recent = (today - d).days <= refresh_recent_days
        cli.fetch(nse_cm_url(d), f"nse_cm/{d:%Y}/{d:%Y%m%d}.csv.zip", refresh_missing=recent)
        cli.fetch(nse_index_url(d), f"nse_indices/{d:%Y}/{d:%Y%m%d}.csv", refresh_missing=recent)
        if bse_start and d >= bse_start:
            cli.fetch(bse_cm_url(d), f"bse_cm/{d:%Y}/{d:%Y%m%d}.csv", refresh_missing=recent)
        n += 1
        if n % progress_every == 0:
            cli.save()
            log_event(log, "download progress", day=str(d), **cli.stats)
    stamp = today.strftime("%Y%m%d")
    for name, path in REFERENCE_FILES.items():
        try:
            cli.fetch(NSE_ARCHIVE + path + f"?d={stamp}", f"reference/{stamp}/{name}")
        except FetchError as e:
            log_event(log, "reference file unavailable", level=30, file=name, error=str(e))
    cli.save()
    log_event(log, "download complete", **cli.stats)
    return dict(cli.stats)


def download_corporate_action_reports(cache_dir: Path, start: date, end: date, min_interval: float = 1.0) -> dict:
    """One NSE PR archive per week (its Bc file covers a rolling ~4-week window of corporate actions).

    Uses its own cache folder/manifest so it can run alongside the price download.
    """
    cli = ArchiveClient(Path(cache_dir) / "pr", min_interval=min_interval)
    today = now_ist().date()
    fridays = pd.date_range(start, end, freq="W-FRI")
    for fri in fridays:
        for back in range(5):                       # Friday, else the latest earlier weekday with a file
            d = (fri - pd.Timedelta(days=back)).date()
            if d > end:
                continue
            got = cli.fetch(nse_pr_url(d), f"{d:%Y}/PR{d:%Y%m%d}.zip", refresh_missing=(today - d).days <= 7)
            if got is not None:
                break
    cli.save()
    log_event(log, "corporate-action report download complete", **cli.stats)
    return dict(cli.stats)


# ============================================================================ parsing
def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype(str).str.replace(",", "").str.strip().replace({"-": None, "": None}),
                         errors="coerce")


def parse_nse_cm(path: Path, d: date) -> pd.DataFrame:
    with zipfile.ZipFile(path) as zf:
        raw = pd.read_csv(zf.open(zf.namelist()[0]), dtype=str)
    raw.columns = [c.strip() for c in raw.columns]
    if "TckrSymb" in raw.columns:
        raw = raw[(raw.get("Sgmt", "CM") == "CM") & (raw.get("FinInstrmTp", "STK") == "STK")]
        df = pd.DataFrame({
            "symbol": raw["TckrSymb"], "series": raw["SctySrs"], "open": _num(raw["OpnPric"]),
            "high": _num(raw["HghPric"]), "low": _num(raw["LwPric"]), "close": _num(raw["ClsPric"]),
            "prevclose": _num(raw["PrvsClsgPric"]), "volume": _num(raw["TtlTradgVol"]),
            "value": _num(raw["TtlTrfVal"]), "isin": raw.get("ISIN"), "file_date": raw["TradDt"]})
    else:
        df = pd.DataFrame({
            "symbol": raw["SYMBOL"], "series": raw["SERIES"], "open": _num(raw["OPEN"]), "high": _num(raw["HIGH"]),
            "low": _num(raw["LOW"]), "close": _num(raw["CLOSE"]), "prevclose": _num(raw["PREVCLOSE"]),
            "volume": _num(raw["TOTTRDQTY"]), "value": _num(raw["TOTTRDVAL"]),
            "isin": raw["ISIN"] if "ISIN" in raw.columns else None, "file_date": raw["TIMESTAMP"]})
    df["symbol"] = df["symbol"].str.strip().str.upper()
    df["series"] = df["series"].str.strip().str.upper()
    fd = pd.to_datetime(df["file_date"].str.strip(), format="mixed", dayfirst=False, errors="coerce")
    df["date"] = pd.Timestamp(d)
    df["date_mismatch"] = fd.notna() & (fd.dt.normalize() != pd.Timestamp(d))
    return df.drop(columns=["file_date"])


INDEX_ALIASES = {
    # historical NSE index names -> current names (index was renamed, series is continuous)
    "NIFTY": "NIFTY50", "NIFTYJUNIOR": "NIFTYNEXT50", "NIFTYMIDCAP": "NIFTYMIDCAP100",
    "NIFTYSMALLCAP": "NIFTYSMALLCAP100", "NIFTYFINANCE": "NIFTYFINANCIALSERVICES",
    "NIFTYDEFTY": "NIFTYDEFTY", "BANKNIFTY": "NIFTYBANK",
}


def normalise_index_name(name: str) -> str:
    """Stable identifier across NSE renames: 'S&P CNX Nifty' / 'CNX Nifty' / 'Nifty 50' -> NIFTY50."""
    n = re.sub(r"[^A-Z0-9]", "", str(name).upper())
    if n.startswith("SPCNX"):
        n = n[5:]
    elif n.startswith("CNX"):
        n = n[3:]
    else:
        return INDEX_ALIASES.get(n, n)
    n = n if n.startswith("NIFTY") else "NIFTY" + n
    return INDEX_ALIASES.get(n, n)


def parse_nse_index(path: Path) -> pd.DataFrame:
    raw = pd.read_csv(path, dtype=str)
    raw.columns = [c.strip() for c in raw.columns]
    df = pd.DataFrame({
        "raw_name": raw["Index Name"].str.strip(),
        "date": pd.to_datetime(raw["Index Date"].str.strip(), format="%d-%m-%Y", errors="coerce"),
        "open": _num(raw["Open Index Value"]), "high": _num(raw["High Index Value"]),
        "low": _num(raw["Low Index Value"]), "close": _num(raw["Closing Index Value"])})
    df["index_name"] = df["raw_name"].map(normalise_index_name)
    bond = df["raw_name"].str.contains(r"G-Sec|Bond|1D Rate|BHARAT Bond|Gilt", case=False, regex=True)
    return df[~bond & df["date"].notna() & df["close"].notna()]


def parse_bse_cm(path: Path, d: date) -> pd.DataFrame:
    raw = pd.read_csv(path, dtype=str)
    raw.columns = [c.strip() for c in raw.columns]
    raw = raw[raw.get("FinInstrmTp", "STK") == "STK"]
    return pd.DataFrame({
        "date": pd.Timestamp(d), "symbol": raw["TckrSymb"].str.strip().str.upper(), "series": raw["SctySrs"].str.strip(),
        "open": _num(raw["OpnPric"]), "high": _num(raw["HghPric"]), "low": _num(raw["LwPric"]),
        "close": _num(raw["ClsPric"]), "prevclose": _num(raw["PrvsClsgPric"]), "volume": _num(raw["TtlTradgVol"]),
        "value": _num(raw["TtlTrfVal"]), "isin": raw["ISIN"], "scrip_code": raw["FinInstrmId"]})


# ============================================================================ build canonical datasets
SERIES_PRIORITY = {"EQ": 0, "BE": 1, "BZ": 2}


def _cached(cli_manifest: dict, prefix: str) -> list[tuple[date, str]]:
    out = []
    for rec in cli_manifest.values():
        if rec.get("status") == 200 and rec.get("path", "").startswith(prefix):
            stem = Path(rec["path"]).name.split(".")[0]
            out.append((datetime.strptime(stem, "%Y%m%d").date(), rec["path"]))
    return sorted(out)


def _latest_reference(cache_dir: Path, name: str) -> Path | None:
    files = sorted((cache_dir / "reference").glob(f"*/{name}"))
    return files[-1] if files else None


def validated_symbol_changes(prices: pd.DataFrame, path: Path | None) -> pd.DataFrame:
    """Use NSE's symbol-change list only where the traded data confirm a clean hand-over."""
    cols = ["symbol", "ex_date", "action_type", "new_symbol", "notes"]
    if path is None:
        return pd.DataFrame(columns=cols)
    sc = pd.read_csv(path, dtype=str, encoding_errors="replace")
    sc.columns = [c.strip().upper() for c in sc.columns]
    sc = sc.rename(columns={"OLD SYMBOL": "old", "NEW SYMBOL": "new", "EFFECTIVE DATE": "eff"})
    sc["old"], sc["new"] = sc["old"].str.strip().str.upper(), sc["new"].str.strip().str.upper()
    sc["eff"] = pd.to_datetime(sc["eff"].str.strip(), format="mixed", dayfirst=True, errors="coerce")
    span = prices.groupby("symbol")["date"].agg(["min", "max"])
    rows = []
    for r in sc.dropna(subset=["eff"]).itertuples(index=False):
        if r.old not in span.index or r.new not in span.index or r.old == r.new:
            continue
        old_last, new_first = span.at[r.old, "max"], span.at[r.new, "min"]
        if old_last < new_first and abs((new_first - r.eff).days) <= 10:
            rows.append({"symbol": r.old, "ex_date": new_first, "action_type": "symbol_change", "new_symbol": r.new,
                         "notes": f"NSE symbolchange.csv effective {r.eff.date()}; validated against traded data"})
    return pd.DataFrame(rows, columns=cols)


T1_SETTLEMENT_FROM = pd.Timestamp("2023-01-27")   # ex-date = record date under T+1 (earlier: record - 1 session)

_NUM = r"(\d+(?:\.\d+)?)"
RE_BONUS = re.compile(r"BONUS\s*" + r"(\d+)\s*:\s*(\d+)")
RE_SPLIT = re.compile(r"SP(?:LI|L)?T\w*\D*?" + _NUM + r"\D+?TO\D*?" + _NUM)
RE_CONSOL = re.compile(r"CONSOLIDAT\w*\D*?" + _NUM + r"\D+?TO\D*?" + _NUM)
RE_RIGHTS = re.compile(r"RIGHTS?\s*(\d+)\s*:\s*(\d+)\s*@?\s*(PREM\w*|PRM)?\s*(?:RS|RE)?\.?\s*" + _NUM)
RE_DIV = re.compile(r"DIV\w*[\s\-:]*(?:RS|RE)\.?\s*-?\s*" + _NUM)


_DATE_RE = re.compile(r"^\s*(\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})?\s*$")


def _read_bc_text(text: str) -> pd.DataFrame:
    """Read a Bc file robustly: some files contain unquoted commas inside the security name or
    the purpose text. Over-long rows are repaired by choosing the split whose date columns parse."""
    import csv

    rows = list(csv.reader(text.splitlines()))
    if not rows:
        return pd.DataFrame()
    header = [h.strip().upper() for h in rows[0]]
    n = len(header)
    date_idx = [i for i, h in enumerate(header) if h.endswith("_DT")]
    out = []
    for f in rows[1:]:
        if not f or all(not x.strip() for x in f):
            continue
        if len(f) > n:
            extra = len(f) - n
            fixed = None
            for pos in (2, n - 1):           # merge surplus into SECURITY, else into PURPOSE
                cand = f[:pos] + [",".join(f[pos:pos + extra + 1])] + f[pos + extra + 1:]
                if all(_DATE_RE.match(cand[i] or "") for i in date_idx):
                    fixed = cand
                    break
            if fixed is None:
                continue
            f = fixed
        elif len(f) < n:
            f = f + [""] * (n - len(f))
        out.append(f)
    return pd.DataFrame(out, columns=header)


def parse_bc(path: Path) -> pd.DataFrame:
    """Parse the Bc (book closure / corporate action) file inside an NSE PR archive."""
    with zipfile.ZipFile(path) as zf:
        name = next((n for n in zf.namelist() if n.lower().startswith("bc") and n.lower().endswith(".csv")), None)
        if name is None:
            return pd.DataFrame()
        text = zf.read(name).decode("latin-1")
    raw = _read_bc_text(text)
    if raw.empty:
        return pd.DataFrame()

    def dt(col):
        v = raw.get(col, pd.Series(index=raw.index, dtype=str)).astype(str).str.strip()
        a = pd.to_datetime(v, format="%d/%m/%Y", errors="coerce")
        return a.fillna(pd.to_datetime(v, format="%Y-%m-%d", errors="coerce"))

    return pd.DataFrame({
        "symbol": raw["SYMBOL"].str.strip().str.upper(), "series": raw["SERIES"].str.strip().str.upper(),
        "record_dt": dt("RECORD_DT"), "bc_start": dt("BC_STRT_DT"), "ex_dt": dt("EX_DT"),
        "purpose": raw["PURPOSE"].astype(str).str.upper().str.replace(r"\s+", " ", regex=True).str.strip()})


def parse_purpose(purpose: str) -> list[dict]:
    """Translate an NSE purpose string into zero or more canonical actions."""
    out = []
    p = purpose.upper()
    if m := RE_BONUS.search(p):
        new, held = float(m.group(1)), float(m.group(2))
        if new > 0 and held > 0:
            out.append({"action_type": "bonus", "ratio_from": held, "ratio_to": new})
    if m := RE_SPLIT.search(p):
        old, new = float(m.group(1)), float(m.group(2))
        if old > new > 0:
            out.append({"action_type": "split", "ratio_from": 1.0, "ratio_to": old / new})
    if m := RE_CONSOL.search(p):
        old, new = float(m.group(1)), float(m.group(2))
        if new > old > 0:
            out.append({"action_type": "consolidation", "adjustment_factor": old / new})
    if m := RE_RIGHTS.search(p):
        out.append({"action_type": "rights", "ratio_from": float(m.group(2)), "ratio_to": float(m.group(1)),
                    "rights_premium_only": bool(m.group(3)), "rights_amount": float(m.group(4))})
    if "DEMERGER" in p:
        out.append({"action_type": "demerger"})
    divs = [float(x) for x in RE_DIV.findall(p)]
    if divs and sum(divs) > 0:
        out.append({"action_type": "dividend", "cash_amount": float(sum(divs))})
    return out


def _estimate_ex(row, sessions: pd.DatetimeIndex) -> pd.Timestamp | None:
    if pd.notna(row.ex_dt):
        return row.ex_dt
    ref = row.record_dt if pd.notna(row.record_dt) else row.bc_start
    if pd.isna(ref):
        return None
    if ref >= T1_SETTLEMENT_FROM:
        return ref
    prior = sessions[sessions < ref]
    return prior[-1] if len(prior) else None


def derive_corporate_actions(bc: pd.DataFrame, prices: pd.DataFrame, sym_map: dict, face_values: dict,
                             tolerance: float = 0.2) -> tuple[pd.DataFrame, dict]:
    """Corporate actions from NSE book-closure files, with ex-dates checked against traded prices.

    Splits/bonus/consolidations are accepted only when the price gap at (or within 3 sessions of)
    the announced ex-date matches the ratio (|log(gap/factor)| < tolerance); the matched session is
    used as the ex-date. Dividends are credited on the first session on/after the ex-date.
    Rights use TERP with rights price = premium + current face value when only the premium is
    published (approximation). Demerger factors are price-implied at the ex-date (approximation).
    """
    stats = {"rows": int(len(bc)), "actions": 0, "verified": 0, "rejected_no_price_match": 0,
             "no_symbol_data": 0, "dividends": 0, "unparsed_dividend_text": 0}
    if bc.empty:
        return pd.DataFrame(), stats
    bc = bc[bc["series"].isin(["EQ", "BE", "BZ"]) | bc["series"].isna()].drop_duplicates(
        ["symbol", "purpose", "ex_dt", "record_dt"])
    px = prices.assign(fsym=prices["symbol"].replace(sym_map)).sort_values(["fsym", "date"])
    by_sym = {s: g.reset_index(drop=True) for s, g in px[["fsym", "date", "open", "close"]].groupby("fsym")}
    sessions = pd.DatetimeIndex(sorted(prices["date"].unique()))
    rows, capital = [], []
    for r in bc.itertuples(index=False):
        acts = parse_purpose(r.purpose)
        if not acts:
            if "DIV" in r.purpose and "RS" not in r.purpose and "RE " not in r.purpose:
                stats["unparsed_dividend_text"] += 1
            continue
        ex = _estimate_ex(r, sessions)
        sym = sym_map.get(r.symbol, r.symbol)
        g = by_sym.get(sym)
        if ex is None or g is None or g.empty:
            stats["no_symbol_data"] += 1
            continue
        k = int(np.searchsorted(g["date"].values, np.datetime64(ex)))
        for a in acts:
            t = a["action_type"]
            base = {"symbol": sym, "notes": f"NSE Bc: {r.purpose}"}
            if t == "dividend":
                if k < len(g):
                    rows.append({**base, "ex_date": g["date"].iloc[k], "action_type": "dividend",
                                 "cash_amount": a["cash_amount"]})
                    stats["dividends"] += 1
                continue
            if t in ("split", "bonus", "consolidation"):
                f = (a["ratio_to"] / a["ratio_from"] if t == "split" else
                     (a["ratio_from"] + a["ratio_to"]) / a["ratio_from"] if t == "bonus" else a["adjustment_factor"])
                capital.append({"sym": sym, "ex": ex, "k": k, "type": t, "f": f, "a": a, "notes": base["notes"]})
                continue
            elif t == "rights" and k < len(g):
                price = a["rights_amount"] + (face_values.get(sym, 0.0) if a["rights_premium_only"] else 0.0)
                rows.append({**base, "ex_date": g["date"].iloc[k], "action_type": "rights",
                             "ratio_from": a["ratio_from"], "ratio_to": a["ratio_to"], "rights_price": price,
                             "notes": base["notes"] + (" | rights price = premium + current FV (approx.)"
                                                       if a["rights_premium_only"] else "")})
            elif t == "demerger" and 0 < k < len(g):
                f = g["close"].iloc[k - 1] / g["open"].iloc[k]
                if 1.02 < f < 10:
                    rows.append({**base, "ex_date": g["date"].iloc[k], "action_type": "demerger",
                                 "adjustment_factor": f, "notes": base["notes"] + " | factor price-implied (approx.)"})
    # ---- capital actions: verify the COMBINED factor of all actions on the same symbol/ex-date
    # (e.g. bonus 4:1 + split 2->1 on the same day = x10 price gap)
    if capital:
        cdf = pd.DataFrame(capital)
        cdf["key"] = [f"{t}|{a.get('ratio_from')}|{a.get('ratio_to')}|{a.get('adjustment_factor')}"
                      for t, a in zip(cdf["type"], cdf["a"])]
        cdf = cdf.drop_duplicates(["sym", "ex", "key"])
        for (sym, ex), grp in cdf.groupby(["sym", "ex"], sort=False):
            g = by_sym[sym]
            k = int(grp["k"].iloc[0])
            f = float(np.prod(grp["f"]))
            logf = abs(np.log(f))
            if logf < 0.25:
                if k >= len(g) or g["date"].iloc[k] != ex:
                    stats["rejected_no_price_match"] += len(grp)
                    continue
                best, how = k, "published ex-date (factor too small to price-verify)"
            else:
                best, best_err = None, np.inf
                for j in range(max(1, k - 3), min(len(g), k + 4)):
                    gap = g["close"].iloc[j - 1] / g["open"].iloc[j]
                    if gap > 0 and np.isfinite(gap):
                        err = abs(np.log(gap / f))
                        if err < best_err:
                            best, best_err = j, err
                if best is None or best_err > min(tolerance, 0.5 * logf):
                    stats["rejected_no_price_match"] += len(grp)
                    continue
                how = f"price-verified (combined factor {f:.3f}, log err {best_err:.3f})"
            for c in grp.itertuples(index=False):
                row = {"symbol": sym, "ex_date": g["date"].iloc[best], "action_type": c.type,
                       "notes": f"{c.notes} | {how}"}
                if c.type == "consolidation":
                    row["adjustment_factor"] = c.f
                else:
                    row.update(ratio_from=c.a["ratio_from"], ratio_to=c.a["ratio_to"])
                rows.append(row)
                stats["verified"] += 1
    out = pd.DataFrame(rows)
    if out.empty:
        return out, stats
    for c in ("ratio_from", "ratio_to", "cash_amount", "rights_price", "adjustment_factor"):
        if c not in out:
            out[c] = np.nan
    div = (out[out["action_type"] == "dividend"].groupby(["symbol", "ex_date"], as_index=False)
           .agg(cash_amount=("cash_amount", "max"), notes=("notes", "first")).assign(action_type="dividend"))
    # the same dividend appears in several weekly files: max (not sum) avoids double counting;
    # interim+special on the same ex-date are already summed inside one purpose string
    other = out[out["action_type"] != "dividend"].drop_duplicates(["symbol", "ex_date", "action_type"])
    out = pd.concat([other, div], ignore_index=True)
    stats["actions"] = int(len(out))
    return out, stats


def detect_missing_sessions(nse: pd.DataFrame, min_fraction: float = 0.3) -> list[date]:
    """Calendar dates of sessions absent from the downloaded weekday files (e.g. weekend Muhurat or
    special Saturday sessions), found from the bhavcopy's unadjusted 'previous close': when a large
    share of stocks report a previous close different from our previous session's close, a session
    happened in between."""
    p = nse[["date", "symbol", "close", "prevclose"]].sort_values(["symbol", "date"])
    p["our_prev"] = p.groupby("symbol")["close"].shift(1)
    p["our_prev_date"] = p.groupby("symbol")["date"].shift(1)
    p = p.dropna(subset=["our_prev", "prevclose"])
    p["mismatch"] = (p["our_prev"] - p["prevclose"]).abs() > 0.0005 * p["prevclose"].abs() + 0.011
    frac = p.groupby("date")["mismatch"].mean()
    sessions = sorted(nse["date"].unique())
    out = []
    for d in frac[frac >= min_fraction].index:
        i = sessions.index(d)
        if i == 0:
            continue
        prev = pd.Timestamp(sessions[i - 1])
        out += [x.date() for x in pd.date_range(prev + pd.Timedelta(days=1), pd.Timestamp(d) - pd.Timedelta(days=1))]
    return out


def download_special_sessions(cache_dir: Path, dates: list[date], min_interval: float = 0.35) -> dict:
    cli = ArchiveClient(cache_dir, min_interval=min_interval)
    for d in dates:
        cli.fetch(nse_cm_url(d), f"nse_cm/{d:%Y}/{d:%Y%m%d}.csv.zip")
        cli.fetch(nse_index_url(d), f"nse_indices/{d:%Y}/{d:%Y%m%d}.csv")
    cli.save()
    return dict(cli.stats)


def build_datasets(cache_dir: Path, out_dir: Path, series_keep=("EQ", "BE", "BZ"), since: date | None = None,
                   **_ignored) -> dict[str, Path]:
    """Parse cached files into canonical Parquet files ready for MarketStore import.

    `since` limits the PRICE/BENCHMARK output to newer rows (incremental daily import); derived
    corporate actions, symbol changes, securities and holidays are always rebuilt from full history.
    """
    cache_dir, out_dir = Path(cache_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((cache_dir / "manifest.json").read_text())
    # ---------------- NSE stock prices
    parts, mismatches = [], 0
    for d, rel in _cached(manifest, "nse_cm/"):
        df = parse_nse_cm(cache_dir / rel, d)
        mismatches += int(df["date_mismatch"].sum())
        parts.append(df[df["series"].isin(series_keep)])
    nse = pd.concat(parts, ignore_index=True)
    nse["prio"] = nse["series"].map(SERIES_PRIORITY).fillna(9)
    nse = nse.sort_values(["date", "symbol", "prio"]).drop_duplicates(["date", "symbol"], keep="first")
    nse = nse.drop(columns=["prio", "date_mismatch"])
    nse.to_parquet(out_dir / "nse_cm_consolidated.parquet", index=False)

    sc = validated_symbol_changes(nse, _latest_reference(cache_dir, "symbolchange.csv"))
    sym_map = dict(zip(sc["symbol"], sc["new_symbol"]))
    for _ in range(5):   # resolve chains
        sym_map = {k: sym_map.get(v, v) for k, v in sym_map.items()}
    face_values = {}
    eq_l0 = _latest_reference(cache_dir, "EQUITY_L.csv")
    if eq_l0 is not None:
        e0 = pd.read_csv(eq_l0, dtype=str, encoding_errors="replace")
        e0.columns = [c.strip().upper() for c in e0.columns]
        face_values = dict(zip(e0["SYMBOL"].str.strip().str.upper(), pd.to_numeric(e0["FACE VALUE"], errors="coerce")))
    pr_manifest_path = cache_dir / "pr" / "manifest.json"
    bc_parts = []
    if pr_manifest_path.exists():
        pr_manifest = json.loads(pr_manifest_path.read_text())
        for rec in pr_manifest.values():
            if rec.get("status") == 200 and rec.get("path"):
                bc_parts.append(parse_bc(cache_dir / "pr" / rec["path"]))
    bc = pd.concat(bc_parts, ignore_index=True) if bc_parts else pd.DataFrame()
    ca, ca_stats = derive_corporate_actions(bc, nse, sym_map, face_values)
    actions = pd.concat([sc, ca], ignore_index=True)
    actions["exchange"] = "NSE"
    weekend_sessions = sorted({d for d in nse["date"].dt.date.unique() if d.weekday() >= 5})

    out_prices = nse if since is None else nse[nse["date"] > pd.Timestamp(since)]
    pr = out_prices.assign(exchange="NSE")[["date", "symbol", "exchange", "open", "high", "low", "close", "volume",
                                             "value", "series"]]

    # ---------------- benchmarks
    iparts = [parse_nse_index(cache_dir / rel) for _, rel in _cached(manifest, "nse_indices/")]
    idx = pd.concat(iparts, ignore_index=True).drop_duplicates(["date", "index_name"], keep="last")
    if since is not None:
        idx = idx[idx["date"] > pd.Timestamp(since)]
    bench = idx[["date", "index_name", "close", "open", "high", "low"]]

    # ---------------- holidays: weekdays where NSE published neither file (within the fetched range)
    cm_status = {u: r["status"] for u, r in manifest.items()}
    cm_dates = [d for d, _ in _cached(manifest, "nse_cm/")]
    hol = []
    if cm_dates:
        for d in weekdays(cm_dates[0], cm_dates[-1]):
            if cm_status.get(nse_cm_url(d)) == 404 and cm_status.get(nse_index_url(d)) in (404, None):
                hol.append({"date": d, "exchange": "NSE", "description": "derived: NSE published no bhavcopy",
                            "source": "nsearchives.nseindia.com (absence of daily files)", "verified": True})
    for d in weekend_sessions:
        hol.append({"date": d, "exchange": "NSE", "description": "special session (weekend trading session)",
                    "source": "nsearchives.nseindia.com bhavcopy published for this date", "verified": True})
    hol.append({"date": date(2026, 12, 25), "exchange": "NSE", "description": "Christmas (2026 holiday list)",
                "source": "NSE 2026 trading-holiday circular (secondary source) - verify", "verified": False})
    holidays = pd.DataFrame(hol).drop_duplicates(["date", "exchange"])

    # ---------------- securities master
    eq_l = _latest_reference(cache_dir, "EQUITY_L.csv")
    master = pd.DataFrame(columns=["symbol", "name", "listing_date", "isin"])
    if eq_l is not None:
        e = pd.read_csv(eq_l, dtype=str, encoding_errors="replace")
        e.columns = [c.strip().upper() for c in e.columns]
        master = pd.DataFrame({"symbol": e["SYMBOL"].str.strip().str.upper(), "name": e["NAME OF COMPANY"].str.strip(),
                               "listing_date": pd.to_datetime(e["DATE OF LISTING"].str.strip(), format="mixed",
                                                              errors="coerce"),
                               "isin": e["ISIN NUMBER"].str.strip()})
    sector = {}
    for nm in ("ind_niftymicrocap250_list.csv", "ind_niftytotalmarket_list.csv", "ind_nifty500list.csv"):
        pth = _latest_reference(cache_dir, nm)
        if pth is not None:
            s = pd.read_csv(pth, dtype=str, encoding_errors="replace")
            sector.update(dict(zip(s["Symbol"].str.strip().str.upper(), s["Industry"].str.strip().str.upper())))
    delisted = {}
    dpath = _latest_reference(cache_dir, "delisted.csv")
    if dpath is not None:
        dl = pd.read_csv(dpath, dtype=str, encoding_errors="replace")
        dl.columns = [c.strip() for c in dl.columns]
        dl["dt"] = pd.to_datetime(dl["Delisted Date"].str.strip(), format="%d-%b-%y", errors="coerce")
        delisted = {s.strip().upper(): (d, t) for s, d, t in zip(dl["Symbol"], dl["dt"], dl["Type of Delisting"])}
    span = nse.assign(fsym=nse["symbol"].replace(sym_map)).groupby("fsym").agg(
        first=("date", "min"), last=("date", "max"), isin=("isin", "last"))
    last_session = nse["date"].max()
    mrec = master.set_index("symbol") if len(master) else master
    srows = []
    for sym, r in span.iterrows():
        name = mrec.at[sym, "name"] if sym in getattr(mrec, "index", []) else None
        listing = mrec.at[sym, "listing_date"] if sym in getattr(mrec, "index", []) else r["first"]
        dl_date, reason = None, None
        if sym in delisted and pd.notna(delisted[sym][0]):
            dl_date, reason = delisted[sym][0], f"NSE delisted list: {delisted[sym][1]}"
        elif r["last"] < last_session - pd.Timedelta(days=45):
            dl_date, reason = r["last"], "derived: no NSE trades since this date (delisting/merger/suspension)"
        srows.append({"symbol": sym, "exchange": "NSE", "isin": r["isin"], "name": name,
                      "sector": sector.get(sym, "UNCLASSIFIED"), "industry": None,
                      "listing_date": listing, "delisting_date": dl_date, "delisting_reason": reason})
    securities = pd.DataFrame(srows)

    # ---------------- current price bands
    bands = pd.DataFrame(columns=["symbol", "start_date", "band_pct", "end_date", "exchange"])
    bpath = _latest_reference(cache_dir, "sec_list.csv")
    if bpath is not None:
        b = pd.read_csv(bpath, dtype=str, encoding_errors="replace")
        b.columns = [c.strip() for c in b.columns]
        b["band_pct"] = pd.to_numeric(b["Band"].str.strip(), errors="coerce") / 100
        b = b[b["band_pct"].notna() & b["Series"].str.strip().isin(series_keep)]
        start = datetime.strptime(bpath.parent.name, "%Y%m%d").date()
        bands = pd.DataFrame({"symbol": b["Symbol"].str.strip().str.upper().replace(sym_map), "start_date": start,
                              "band_pct": b["band_pct"], "end_date": None, "exchange": "NSE"})
        bands = bands.drop_duplicates(["symbol"])

    # ---------------- BSE (cross-check only)
    bparts = [parse_bse_cm(cache_dir / rel, d) for d, rel in _cached(manifest, "bse_cm/")]
    bse = pd.concat(bparts, ignore_index=True) if bparts else pd.DataFrame()
    if len(bse):
        bse = bse.drop_duplicates(["date", "symbol"], keep="first")
        bse.to_parquet(out_dir / "bse_cm_consolidated.parquet", index=False)
        bse_out = bse if since is None else bse[bse["date"] > pd.Timestamp(since)]
        bse_out = bse_out.assign(exchange="BSE")[["date", "symbol", "exchange", "open", "high", "low", "close",
                                                  "volume", "value", "series"]]
        bse_out.to_parquet(out_dir / "prices_bse.parquet", index=False)

    paths = {
        "prices": out_dir / "prices_nse.parquet", "benchmarks": out_dir / "benchmarks_nse.parquet",
        "corporate_actions": out_dir / "corporate_actions_nse.parquet", "securities": out_dir / "securities_nse.parquet",
        "holidays": out_dir / "holidays_nse.parquet", "price_bands": out_dir / "price_bands_nse.parquet",
    }
    pr.to_parquet(paths["prices"], index=False)
    bench.to_parquet(paths["benchmarks"], index=False)
    actions.to_parquet(paths["corporate_actions"], index=False)
    securities.to_parquet(paths["securities"], index=False)
    holidays.to_parquet(paths["holidays"], index=False)
    bands.to_parquet(paths["price_bands"], index=False)
    if len(bse):
        paths["prices_bse"] = out_dir / "prices_bse.parquet"
    summary = {"nse_rows": int(len(nse)), "nse_symbols": int(nse["symbol"].nunique()),
               "nse_first": str(nse["date"].min().date()), "nse_last": str(nse["date"].max().date()),
               "file_date_mismatch_rows": mismatches, "symbol_changes": int(len(sc)),
               "corporate_actions": ca_stats, "special_sessions": [str(d) for d in weekend_sessions],
               "holidays": int(len(holidays)),
               "securities": int(len(securities)), "delisted_or_stopped": int(securities["delisting_date"].notna().sum()),
               "indices": int(idx["index_name"].nunique()), "bse_rows": int(len(bse))}
    (out_dir / "build_summary.json").write_text(json.dumps(summary, indent=2))
    log_event(log, "exchange datasets built", **summary)
    return paths


def cross_check_nse_bse(out_dir: Path, tolerance: float = 0.02) -> pd.DataFrame:
    """Compare NSE vs BSE closes by ISIN for overlapping dates (independent data-quality check)."""
    out_dir = Path(out_dir)
    n = pd.read_parquet(out_dir / "nse_cm_consolidated.parquet", columns=["date", "symbol", "close", "isin"])
    b_path = out_dir / "bse_cm_consolidated.parquet"
    if not b_path.exists():
        return pd.DataFrame()
    b = pd.read_parquet(b_path, columns=["date", "close", "isin"])
    m = n.dropna(subset=["isin"]).merge(b.dropna(subset=["isin"]), on=["date", "isin"], suffixes=("_nse", "_bse"))
    m["rel_diff"] = (m["close_nse"] / m["close_bse"] - 1).abs()
    flagged = m[m["rel_diff"] > tolerance].sort_values("rel_diff", ascending=False)
    summary = {"pairs_compared": int(len(m)), "flagged": int(len(flagged)),
               "median_abs_diff": float(m["rel_diff"].median()) if len(m) else None,
               "p99_abs_diff": float(m["rel_diff"].quantile(0.99)) if len(m) else None}
    (out_dir / "nse_bse_crosscheck.json").write_text(json.dumps(summary, indent=2))
    flagged.head(5000).to_csv(out_dir / "nse_bse_crosscheck_flagged.csv", index=False)
    return flagged


