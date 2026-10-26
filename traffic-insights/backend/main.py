"""
Traffic Insights - FastAPI backend.

Serves the dashboard (frontend/) and a REST API at http://127.0.0.1:8000.

Data model
----------
* One "active dataset" of hourly traffic records is kept in memory (pandas) and persisted in SQLite.
* On first start it is filled with SYNTHETIC demo data (data/sample_traffic.csv).
* "Import Spark Results" replaces it with the contents of traffic_insights_results.zip
  produced by the Colab notebook. "Reset" goes back to the demo data.

IMPORTANT: Congestion levels (Low / Moderate / High / Severe) are ESTIMATES derived only from
traffic volume. They are not measured speeds or travel times.

Run (from the project root):
    uvicorn backend.main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import sqlite3
import threading
import zipfile
import zlib
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

APP_VERSION = "1.0.0"
ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("traffic_insights")


# --------------------------------------------------------------------------------------
# Configuration (tiny .env reader so beginners need no extra package)
# --------------------------------------------------------------------------------------
def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


MAX_UPLOAD_MB = _env_int("MAX_UPLOAD_MB", 50)
MAX_UNCOMPRESSED_MB = _env_int("MAX_UNCOMPRESSED_MB", 200)
MAX_ZIP_MEMBERS = 200

# --------------------------------------------------------------------------------------
# Domain constants
# --------------------------------------------------------------------------------------
LEVEL_ORDER = ["Low", "Moderate", "High", "Severe"]
# Volume (vehicles/hour) thresholds. ESTIMATES only - keep identical to the notebook.
THRESHOLD_TEXT = {"Low": "< 1500", "Moderate": "1500-3499", "High": "3500-4999", "Severe": ">= 5000"}
DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DISCLAIMER = (
    "Congestion levels are ESTIMATES derived only from traffic volume (vehicles per hour) using fixed "
    "thresholds. They are not measured speeds, delays or travel times."
)
DEMO_SOURCE = "Demo data (SYNTHETIC) - randomly generated for the UI, NOT real traffic measurements"

REQUIRED_FILES = [
    "traffic_summary.json",
    "hourly_trends.csv",
    "daily_trends.csv",
    "monthly_trends.csv",
    "congestion_distribution.csv",
    "cleaned_traffic.csv",
]
REQUIRED_COLUMNS = {
    "hourly_trends.csv": ["hour", "avg_volume", "max_volume", "min_volume", "record_count"],
    "daily_trends.csv": ["date", "avg_volume", "max_volume", "record_count"],
    "monthly_trends.csv": ["year_month", "avg_volume", "max_volume", "record_count"],
    "congestion_distribution.csv": ["congestion_level", "record_count", "percentage"],
    "cleaned_traffic.csv": ["date_time", "traffic_volume", "congestion_level", "holiday", "temp_c", "weather_main"],
}
SUMMARY_REQUIRED_KEYS = [
    "total_records", "avg_traffic_volume", "max_traffic_volume", "min_traffic_volume",
    "peak_hour", "date_start", "date_end",
]
RECORD_COLUMNS = [
    "date_time", "holiday", "temp_c", "rain_1h", "snow_1h", "clouds_all",
    "weather_main", "weather_description", "traffic_volume", "congestion_level",
]
STORED_COLUMNS = RECORD_COLUMNS + ["date", "hour", "day_of_week", "month", "year_month"]
SORTABLE = set(RECORD_COLUMNS)
EXPORT_DATASETS = ["records", "hourly", "daily", "weekday", "monthly", "congestion", "weather"]


class ApiError(Exception):
    """An error with a friendly message that is sent to the browser as JSON."""

    def __init__(self, message: str, status: int = 400, details: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.details = details or []


# --------------------------------------------------------------------------------------
# Pure calculation helpers (these are what the unit tests exercise)
# --------------------------------------------------------------------------------------
def congestion_level(volume: float) -> str:
    """Estimated congestion category from hourly traffic volume."""
    if volume < 1500:
        return "Low"
    if volume < 3500:
        return "Moderate"
    if volume < 5000:
        return "High"
    return "Severe"


def to_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """DataFrame -> list of JSON-safe dicts (NaN becomes null)."""
    return json.loads(df.to_json(orient="records"))


def _num(value: Any, digits: int = 2) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), digits)


def compute_summary(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {
            "empty": True, "total_records": 0, "avg_traffic_volume": None, "median_traffic_volume": None,
            "max_traffic_volume": None, "min_traffic_volume": None, "peak_hour": None, "quietest_hour": None,
            "busiest_day_of_week": None, "busiest_month": None, "high_severe_pct": None,
            "max_volume_at": None, "date_start": None, "date_end": None, "days_covered": 0,
        }
    by_hour = df.groupby("hour")["traffic_volume"].mean()
    by_day = df.groupby("day_of_week")["traffic_volume"].mean()
    by_month = df.groupby("year_month")["traffic_volume"].mean()
    high_severe = df["congestion_level"].isin(["High", "Severe"]).mean() * 100
    top = df.loc[df["traffic_volume"].idxmax()]
    return {
        "empty": False,
        "total_records": int(len(df)),
        "avg_traffic_volume": _num(df["traffic_volume"].mean()),
        "median_traffic_volume": _num(df["traffic_volume"].median()),
        "max_traffic_volume": int(df["traffic_volume"].max()),
        "min_traffic_volume": int(df["traffic_volume"].min()),
        "peak_hour": int(by_hour.idxmax()),
        "quietest_hour": int(by_hour.idxmin()),
        "busiest_day_of_week": str(by_day.idxmax()),
        "busiest_month": str(by_month.idxmax()),
        "high_severe_pct": _num(high_severe),
        "max_volume_at": str(top["date_time"]),
        "date_start": str(df["date"].min()),
        "date_end": str(df["date"].max()),
        "days_covered": int(df["date"].nunique()),
    }


def _agg(df: pd.DataFrame, key: str) -> pd.DataFrame:
    out = df.groupby(key)["traffic_volume"].agg(
        avg_volume="mean", max_volume="max", min_volume="min", record_count="count"
    )
    out["avg_volume"] = out["avg_volume"].round(2)
    return out.reset_index()


def hourly_trend(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["hour", "avg_volume", "max_volume", "min_volume", "record_count"])
    return _agg(df, "hour").sort_values("hour")


def daily_trend(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["date", "avg_volume", "max_volume", "min_volume", "record_count"])
    return _agg(df, "date").sort_values("date")


def monthly_trend(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["year_month", "avg_volume", "max_volume", "min_volume", "record_count"])
    return _agg(df, "year_month").sort_values("year_month")


def weekday_trend(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["day_of_week", "avg_volume", "max_volume", "min_volume", "record_count"])
    out = _agg(df, "day_of_week")
    out["order"] = out["day_of_week"].map({d: i for i, d in enumerate(DAY_ORDER)})
    return out.sort_values("order").drop(columns="order")


def congestion_distribution(df: pd.DataFrame) -> pd.DataFrame:
    counts = df["congestion_level"].value_counts() if not df.empty else pd.Series(dtype="int64")
    out = pd.DataFrame({"congestion_level": LEVEL_ORDER})
    out["record_count"] = out["congestion_level"].map(counts).fillna(0).astype(int)
    total = out["record_count"].sum()
    out["percentage"] = (out["record_count"] / total * 100).round(2) if total else 0.0
    return out


def weather_breakdown(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["weather_main", "avg_volume", "max_volume", "min_volume", "record_count"])
    return _agg(df, "weather_main").sort_values("avg_volume", ascending=False)


TREND_FUNCS = {
    "hourly": hourly_trend, "daily": daily_trend, "weekday": weekday_trend, "monthly": monthly_trend,
    "congestion": congestion_distribution, "weather": weather_breakdown,
}


def filter_dates(df: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    if start:
        df = df[df["date"] >= start]
    if end:
        df = df[df["date"] <= end]
    return df


def query_records(
    df: pd.DataFrame, search: str | None = None, level: str | None = None,
    sort: str = "date_time", order: str = "asc",
) -> pd.DataFrame:
    if level:
        df = df[df["congestion_level"] == level]
    if search and search.strip():
        needle = search.strip()
        mask = pd.Series(False, index=df.index)
        for col in ["date_time", "holiday", "weather_main", "weather_description", "congestion_level", "traffic_volume"]:
            mask |= df[col].astype(str).str.contains(needle, case=False, regex=False)
        df = df[mask]
    ascending = order != "desc"
    if sort == "congestion_level":
        key = df["congestion_level"].map({lvl: i for i, lvl in enumerate(LEVEL_ORDER)})
        df = df.assign(_k=key).sort_values("_k", ascending=ascending, kind="stable").drop(columns="_k")
    else:
        df = df.sort_values(sort, ascending=ascending, kind="stable", na_position="last")
    return df[RECORD_COLUMNS]


def csv_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Prevent CSV/Excel formula injection from imported text cells."""
    df = df.copy()
    for col in df.columns:
        if df[col].dtype == object or pd.api.types.is_string_dtype(df[col]):
            df[col] = df[col].map(
                lambda v: "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v
            )
    return df


# --------------------------------------------------------------------------------------
# Validation + normalisation of the cleaned records
# --------------------------------------------------------------------------------------
def _examples(index: pd.Index, limit: int = 5) -> str:
    rows = [str(int(i) + 2) for i in list(index)[:limit]]  # +2: header line + 0-based index
    return "row(s) " + ", ".join(rows) + (" ..." if len(index) > limit else "")


def prepare_records(raw: pd.DataFrame, source_name: str = "cleaned_traffic.csv") -> pd.DataFrame:
    """Validate raw records and return a normalised DataFrame. Raises ApiError with details."""
    errors: list[str] = []
    missing = [c for c in REQUIRED_COLUMNS["cleaned_traffic.csv"] if c not in raw.columns]
    if missing:
        raise ApiError(f"{source_name}: missing required column(s): {', '.join(missing)}", details=[
            f"Required columns: {', '.join(REQUIRED_COLUMNS['cleaned_traffic.csv'])}",
        ])
    if raw.empty:
        raise ApiError(f"{source_name} has a header but no data rows (empty data).")

    df = pd.DataFrame(index=raw.index)
    ts = pd.to_datetime(raw["date_time"].astype(str).str.strip(), errors="coerce", format="ISO8601")
    bad_ts = ts.isna()
    if bad_ts.any():
        errors.append(f"{int(bad_ts.sum())} invalid date_time value(s) ({_examples(raw.index[bad_ts])}); expected YYYY-MM-DD HH:MM:SS")

    vol = pd.to_numeric(raw["traffic_volume"], errors="coerce")
    bad_vol = vol.isna() | (vol < 0) | (vol > 100000)
    if bad_vol.any():
        errors.append(f"{int(bad_vol.sum())} invalid traffic_volume value(s) ({_examples(raw.index[bad_vol])}); must be a number between 0 and 100000")

    level = raw["congestion_level"].astype(str).str.strip().str.capitalize()
    bad_level = ~level.isin(LEVEL_ORDER)
    if bad_level.any():
        sample = ", ".join(sorted({repr(v) for v in raw.loc[bad_level, "congestion_level"].astype(str).head(20)})[:5])
        errors.append(f"{int(bad_level.sum())} invalid congestion_level value(s), e.g. {sample}; allowed: {', '.join(LEVEL_ORDER)}")

    if errors:
        raise ApiError(f"{source_name} failed validation.", details=errors)

    df["date_time"] = ts.dt.strftime("%Y-%m-%d %H:%M:%S")
    df["date"] = ts.dt.strftime("%Y-%m-%d")
    df["hour"] = ts.dt.hour.astype(int)
    df["day_of_week"] = ts.dt.day_name()
    df["month"] = ts.dt.month.astype(int)
    df["year_month"] = ts.dt.strftime("%Y-%m")
    df["traffic_volume"] = vol.round().astype(int)
    df["congestion_level"] = level
    df["holiday"] = raw["holiday"].fillna("None").astype(str).str.strip().replace("", "None")
    df["weather_main"] = raw["weather_main"].fillna("Unknown").astype(str).str.strip().replace("", "Unknown")
    desc = raw["weather_description"] if "weather_description" in raw.columns else raw["weather_main"]
    df["weather_description"] = desc.fillna("Unknown").astype(str).str.strip()
    df["temp_c"] = pd.to_numeric(raw["temp_c"], errors="coerce").round(2)
    for col in ["rain_1h", "snow_1h", "clouds_all"]:
        df[col] = pd.to_numeric(raw[col], errors="coerce") if col in raw.columns else float("nan")
    df = df.sort_values("date_time", kind="stable").reset_index(drop=True)
    return df[STORED_COLUMNS]


# --------------------------------------------------------------------------------------
# ZIP import (never extracts to disk; everything is read in memory with size limits)
# --------------------------------------------------------------------------------------
def _read_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    with zf.open(info) as fh:
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise ApiError(f"'{info.filename}' is larger than the allowed {limit // (1024 * 1024)} MB once unpacked.", 413)
    return data


def _read_csv(name: str, data: bytes, errors: list[str]) -> pd.DataFrame | None:
    required = REQUIRED_COLUMNS[name]
    try:
        text = data.decode("utf-8-sig")
        df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False, na_values=[""])
    except UnicodeDecodeError:
        errors.append(f"{name}: file is not valid UTF-8 text.")
        return None
    except (pd.errors.EmptyDataError, pd.errors.ParserError) as exc:
        errors.append(f"{name}: cannot be read as CSV ({str(exc).splitlines()[0]}).")
        return None
    df.columns = [str(c).strip() for c in df.columns]
    missing = [c for c in required if c not in df.columns]
    if missing:
        errors.append(f"{name}: missing column(s): {', '.join(missing)} (found: {', '.join(df.columns) or 'none'})")
        return None
    if df.empty:
        errors.append(f"{name}: has a header but no data rows (empty data).")
        return None
    return df


def _check_numeric(df: pd.DataFrame, name: str, col: str, lo: float | None = None, hi: float | None = None) -> list[str]:
    values = pd.to_numeric(df[col], errors="coerce")
    bad = values.isna()
    if lo is not None:
        bad |= values < lo
    if hi is not None:
        bad |= values > hi
    if bad.any():
        return [f"{name}: column '{col}' has {int(bad.sum())} invalid value(s) ({_examples(df.index[bad])})"]
    return []


def parse_results_zip(data: bytes) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Validate and parse traffic_insights_results.zip. Returns (records, meta)."""
    if not data:
        raise ApiError("The uploaded file is empty.")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise ApiError("The uploaded file is not a valid ZIP archive.", details=["Upload traffic_insights_results.zip created by the Colab notebook."])
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ApiError("The ZIP archive is corrupted.") from None

    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/")]
        if len(infos) > MAX_ZIP_MEMBERS:
            raise ApiError(f"The ZIP contains too many files ({len(infos)}).")
        if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED_MB * 1024 * 1024:
            raise ApiError(f"The ZIP would unpack to more than {MAX_UNCOMPRESSED_MB} MB.", 413)
        for info in infos:
            parts = info.filename.replace("\\", "/").split("/")
            if ".." in parts or info.filename.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", info.filename):
                raise ApiError(f"Unsafe file path inside ZIP: '{info.filename}'.")
            if info.flag_bits & 0x1:
                raise ApiError("Password-protected ZIP files are not supported.")

        by_name: dict[str, list[zipfile.ZipInfo]] = {}
        for info in infos:  # allow files inside one folder: results/hourly_trends.csv
            by_name.setdefault(info.filename.replace("\\", "/").split("/")[-1], []).append(info)
        missing = [f for f in REQUIRED_FILES if f not in by_name]
        if missing:
            raise ApiError(
                f"The ZIP is missing required file(s): {', '.join(missing)}",
                details=[f"Required files: {', '.join(REQUIRED_FILES)}", f"Found: {', '.join(sorted(by_name)) or 'nothing'}"],
            )
        dupes = [f for f in REQUIRED_FILES if len(by_name[f]) > 1]
        if dupes:
            raise ApiError(f"The ZIP contains duplicate copies of: {', '.join(dupes)}")

        limit = MAX_UNCOMPRESSED_MB * 1024 * 1024
        try:
            blobs = {f: _read_member(zf, by_name[f][0], limit) for f in REQUIRED_FILES}
        except (zipfile.BadZipFile, RuntimeError, zlib.error) as exc:
            raise ApiError(f"The ZIP is corrupted or unreadable ({exc}).") from None

    errors: list[str] = []
    warnings: list[str] = []

    # --- summary JSON
    summary: dict[str, Any] = {}
    try:
        parsed = json.loads(blobs["traffic_summary.json"].decode("utf-8-sig"))
        if not isinstance(parsed, dict):
            errors.append("traffic_summary.json: must contain a JSON object.")
        else:
            summary = parsed
            missing_keys = [k for k in SUMMARY_REQUIRED_KEYS if k not in summary]
            if missing_keys:
                errors.append(f"traffic_summary.json: missing key(s): {', '.join(missing_keys)}")
            for k in ["total_records", "avg_traffic_volume", "max_traffic_volume", "min_traffic_volume", "peak_hour"]:
                v = summary.get(k)
                if k in summary and (isinstance(v, bool) or not isinstance(v, (int, float))):
                    errors.append(f"traffic_summary.json: '{k}' must be a number.")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"traffic_summary.json: not valid JSON ({exc}).")

    # --- aggregate CSVs
    frames = {n: _read_csv(n, blobs[n], errors) for n in REQUIRED_COLUMNS}
    h, d, m, c = (frames[n] for n in ["hourly_trends.csv", "daily_trends.csv", "monthly_trends.csv", "congestion_distribution.csv"])
    if h is not None:
        for col, lo, hi in [("hour", 0, 23), ("avg_volume", 0, None), ("max_volume", 0, None), ("min_volume", 0, None), ("record_count", 0, None)]:
            errors += _check_numeric(h, "hourly_trends.csv", col, lo, hi)
    if d is not None:
        for col in ["avg_volume", "max_volume", "record_count"]:
            errors += _check_numeric(d, "daily_trends.csv", col, 0)
        bad = pd.to_datetime(d["date"], errors="coerce", format="ISO8601").isna()
        if bad.any():
            errors.append(f"daily_trends.csv: column 'date' has {int(bad.sum())} invalid value(s) ({_examples(d.index[bad])})")
    if m is not None:
        for col in ["avg_volume", "max_volume", "record_count"]:
            errors += _check_numeric(m, "monthly_trends.csv", col, 0)
        bad = ~m["year_month"].astype(str).str.match(r"^\d{4}-(0[1-9]|1[0-2])$")
        if bad.any():
            errors.append(f"monthly_trends.csv: column 'year_month' must look like 2017-03 ({_examples(m.index[bad])})")
    if c is not None:
        errors += _check_numeric(c, "congestion_distribution.csv", "record_count", 0)
        errors += _check_numeric(c, "congestion_distribution.csv", "percentage", 0, 100)
        bad_lv = ~c["congestion_level"].astype(str).str.strip().str.capitalize().isin(LEVEL_ORDER)
        if bad_lv.any():
            errors.append(f"congestion_distribution.csv: unknown congestion_level value(s); allowed: {', '.join(LEVEL_ORDER)}")

    records = None
    cleaned = frames["cleaned_traffic.csv"]
    if cleaned is not None:
        try:
            records = prepare_records(cleaned)
        except ApiError as exc:
            errors.append(exc.message)
            errors.extend(f"  - {x}" for x in exc.details)

    if errors:
        raise ApiError("The ZIP failed validation. Nothing was imported.", details=errors)
    assert records is not None

    # --- cross-checks (warnings only: they do not block the import)
    n = len(records)
    if int(summary["total_records"]) != n:
        warnings.append(f"traffic_summary.json says total_records={int(summary['total_records'])} but cleaned_traffic.csv has {n} rows.")
    for name, frame in [("hourly_trends.csv", h), ("congestion_distribution.csv", c)]:
        total = int(pd.to_numeric(frame["record_count"]).sum())
        if total != n:
            warnings.append(f"{name} record_count adds up to {total}, but cleaned_traffic.csv has {n} rows.")
    if records["congestion_level"].isin(LEVEL_ORDER).all():
        wrong = (records["congestion_level"] != records["traffic_volume"].map(congestion_level)).sum()
        if wrong:
            warnings.append(f"{int(wrong)} row(s) use congestion levels that differ from this app's volume thresholds; the file's labels are shown as-is.")

    meta = {
        "kind": "imported",
        "source": str(summary.get("source") or "Imported Spark results (traffic_insights_results.zip)")[:300],
        "is_synthetic": bool(summary.get("is_synthetic", False)),
        "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "spark_summary": summary,
        "warnings": warnings,
    }
    return records, meta


# --------------------------------------------------------------------------------------
# Storage: in-memory pandas + SQLite persistence (atomic replace)
# --------------------------------------------------------------------------------------
class Store:
    def __init__(self, db_path: Path, sample_path: Path):
        self.db_path = Path(db_path)
        self.sample_path = Path(sample_path)
        self.lock = threading.RLock()
        self.df: pd.DataFrame = pd.DataFrame(columns=STORED_COLUMNS)
        self.meta: dict[str, Any] = {}
        self._startup()

    def _startup(self) -> None:
        if self.db_path.is_file():
            try:
                self._load_db()
                return
            except Exception as exc:  # corrupt/old DB -> start over with demo data
                log.warning("Could not read %s (%s); loading demo data instead.", self.db_path, exc)
        self.reset_demo()

    def _load_db(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as con:
            df = pd.read_sql_query("SELECT * FROM records", con)
            meta_rows = con.execute("SELECT value FROM meta WHERE key = 'meta'").fetchone()
        if meta_rows is None or not set(STORED_COLUMNS).issubset(df.columns) or df.empty:
            raise ValueError("database is incomplete")
        self.df = df[STORED_COLUMNS]
        self.meta = json.loads(meta_rows[0])

    def _persist(self, df: pd.DataFrame, meta: dict[str, Any]) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.db_path.with_suffix(self.db_path.suffix + ".tmp")
        if tmp.exists():
            tmp.unlink()
        with closing(sqlite3.connect(tmp)) as con:
            df.to_sql("records", con, index=False)
            con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            con.execute("INSERT INTO meta VALUES ('meta', ?)", (json.dumps(meta),))
            con.commit()
        os.replace(tmp, self.db_path)  # atomic: old data stays intact if anything above failed

    def replace(self, df: pd.DataFrame, meta: dict[str, Any]) -> None:
        with self.lock:
            self._persist(df, meta)
            self.df, self.meta = df, meta

    def reset_demo(self) -> None:
        if not self.sample_path.is_file():
            raise ApiError(f"Demo data file not found: {self.sample_path}", 500)
        raw = pd.read_csv(self.sample_path, dtype=str, keep_default_na=False, na_values=[""])
        df = prepare_records(raw, self.sample_path.name)
        meta = {
            "kind": "demo", "source": DEMO_SOURCE, "is_synthetic": True,
            "imported_at": None, "spark_summary": None, "warnings": [],
        }
        self.replace(df, meta)


# --------------------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------------------
def create_app(db_path: str | Path | None = None, sample_path: str | Path | None = None,
               frontend_dir: str | Path | None = None) -> FastAPI:
    db_path = Path(db_path or os.environ.get("TRAFFIC_DB_PATH") or ROOT / "data" / "traffic.db")
    sample_path = Path(sample_path or ROOT / "data" / "sample_traffic.csv")
    frontend_dir = Path(frontend_dir or ROOT / "frontend")
    store = Store(db_path, sample_path)

    app = FastAPI(title="Traffic Insights API", version=APP_VERSION,
                  description="Traffic congestion analysis dashboard backend (Spark results + demo data).")
    app.state.store = store

    # ---------- error handling: every error is {"error": str, "details": [str]}
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return JSONResponse({"error": exc.message, "details": exc.details}, status_code=exc.status)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        return JSONResponse({"error": str(exc.detail), "details": []}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        details = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        return JSONResponse({"error": "Invalid request parameters.", "details": details}, status_code=422)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception):
        log.exception("Unhandled error")
        return JSONResponse({"error": "Internal server error.", "details": []}, status_code=500)

    # ---------- helpers
    def parse_filters(start: str | None, end: str | None) -> tuple[str | None, str | None]:
        out = []
        for label, value in (("start", start), ("end", end)):
            if value in (None, ""):
                out.append(None)
                continue
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                raise ApiError(f"Invalid '{label}' date '{value}'. Use YYYY-MM-DD.") from None
            out.append(value)
        if out[0] and out[1] and out[0] > out[1]:
            raise ApiError("The start date must not be after the end date.")
        return out[0], out[1]

    def current(start: str | None, end: str | None) -> tuple[pd.DataFrame, dict[str, Any]]:
        s, e = parse_filters(start, end)
        with store.lock:
            df = store.df
        return filter_dates(df, s, e), {"start": s, "end": e}

    def dataset_info() -> dict[str, Any]:
        with store.lock:
            df, meta = store.df, dict(store.meta)
        return {
            **meta,
            "rows": int(len(df)),
            "date_min": str(df["date"].min()) if len(df) else None,
            "date_max": str(df["date"].max()) if len(df) else None,
            "thresholds": THRESHOLD_TEXT,
            "disclaimer": DISCLAIMER,
        }

    def csv_response(df: pd.DataFrame, filename: str) -> Response:
        body = csv_safe(df).to_csv(index=False)
        return Response(
            content="﻿" + body,  # BOM so Excel on Windows opens UTF-8 correctly
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ---------- API routes
    @app.get("/api/health", tags=["system"])
    def health():
        with store.lock:
            rows = int(len(store.df))
            kind = store.meta.get("kind")
        return {"status": "ok", "version": APP_VERSION, "rows_loaded": rows, "dataset_kind": kind,
                "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    @app.get("/api/dataset", tags=["data"])
    def dataset():
        return dataset_info()

    @app.get("/api/summary", tags=["analytics"])
    def summary(start: str | None = None, end: str | None = None):
        df, filters = current(start, end)
        return {"filters": filters, "data": compute_summary(df)}

    @app.get("/api/trends/{kind}", tags=["analytics"])
    def trends(kind: str, start: str | None = None, end: str | None = None):
        if kind not in ("hourly", "daily", "weekday", "monthly"):
            raise ApiError("Unknown trend. Use hourly, daily, weekday or monthly.", 404)
        df, filters = current(start, end)
        data = to_records(TREND_FUNCS[kind](df))
        return {"filters": filters, "data": data, "count": len(data)}

    @app.get("/api/breakdown/{kind}", tags=["analytics"])
    def breakdown(kind: str, start: str | None = None, end: str | None = None):
        if kind not in ("congestion", "weather"):
            raise ApiError("Unknown breakdown. Use congestion or weather.", 404)
        df, filters = current(start, end)
        data = to_records(TREND_FUNCS[kind](df))
        return {"filters": filters, "data": data, "count": len(data), "disclaimer": DISCLAIMER if kind == "congestion" else None}

    @app.get("/api/records", tags=["data"])
    def records(start: str | None = None, end: str | None = None, search: str | None = None,
                level: str | None = None, sort: str = "date_time", order: str = "asc",
                page: int = 1, page_size: int = 25):
        if sort not in SORTABLE:
            raise ApiError(f"Cannot sort by '{sort}'.", details=[f"Allowed: {', '.join(sorted(SORTABLE))}"])
        if order not in ("asc", "desc"):
            raise ApiError("order must be 'asc' or 'desc'.")
        if level and level not in LEVEL_ORDER:
            raise ApiError(f"level must be one of: {', '.join(LEVEL_ORDER)}")
        if page < 1 or not 1 <= page_size <= 500:
            raise ApiError("page must be >= 1 and page_size between 1 and 500.")
        df, filters = current(start, end)
        result = query_records(df, search, level, sort, order)
        total = int(len(result))
        pages = max(1, -(-total // page_size))
        chunk = result.iloc[(page - 1) * page_size: page * page_size]
        return {"filters": {**filters, "search": search, "level": level}, "data": to_records(chunk),
                "total": total, "page": page, "page_size": page_size, "pages": pages}

    @app.get("/api/export/{dataset_name}", tags=["data"])
    def export(dataset_name: str, start: str | None = None, end: str | None = None,
               search: str | None = None, level: str | None = None,
               sort: str = "date_time", order: str = "asc"):
        if dataset_name not in EXPORT_DATASETS:
            raise ApiError(f"Unknown export. Use one of: {', '.join(EXPORT_DATASETS)}", 404)
        df, _ = current(start, end)
        if dataset_name == "records":
            if sort not in SORTABLE or order not in ("asc", "desc") or (level and level not in LEVEL_ORDER):
                raise ApiError("Invalid sort, order or level parameter.")
            out = query_records(df, search, level, sort, order)
        else:
            out = TREND_FUNCS[dataset_name](df)
        return csv_response(out, f"traffic_{dataset_name}.csv")

    @app.post("/api/import", tags=["import"])
    async def import_results(file: UploadFile = File(...)):
        limit = MAX_UPLOAD_MB * 1024 * 1024
        chunks, size = [], 0
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise ApiError(f"The file is larger than the {MAX_UPLOAD_MB} MB upload limit.", 413)
            chunks.append(chunk)
        records_df, meta = parse_results_zip(b"".join(chunks))
        meta["upload_filename"] = os.path.basename(file.filename or "upload.zip")[:200]
        store.replace(records_df, meta)  # only reached when every check passed
        return {"message": "Import successful.", "rows": int(len(records_df)), "warnings": meta["warnings"],
                "dataset": dataset_info()}

    @app.post("/api/reset", tags=["import"])
    def reset():
        store.reset_demo()
        return {"message": "Demo data restored.", "dataset": dataset_info()}

    # ---------- frontend (must be mounted last so /api/* routes win)
    if frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
    return app


app = create_app()

if __name__ == "__main__":  # python -m backend.main
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=_env_int("PORT", 8000))
