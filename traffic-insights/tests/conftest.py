import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# The module-level `app` in backend.main is created at import time; keep it away from the real database.
os.environ.setdefault("TRAFFIC_DB_PATH", str(Path(tempfile.mkdtemp()) / "import_time.db"))

from fastapi.testclient import TestClient  # noqa: E402

from backend.main import create_app  # noqa: E402

REQUIRED_FILES = [
    "traffic_summary.json", "hourly_trends.csv", "daily_trends.csv",
    "monthly_trends.csv", "congestion_distribution.csv", "cleaned_traffic.csv",
]


def small_cleaned() -> pd.DataFrame:
    """A tiny, hand-checkable dataset (2 days, 3 hours each)."""
    rows = [
        ("2020-01-06 07:00:00", "None", 1.0, "Clear", 1000),   # Mon, Low
        ("2020-01-06 08:00:00", "None", 2.0, "Rain", 4000),    # Mon, High
        ("2020-01-06 09:00:00", "None", 3.0, "Clear", 6000),   # Mon, Severe
        ("2020-01-07 07:00:00", "None", 1.0, "Clear", 2000),   # Tue, Moderate
        ("2020-01-07 08:00:00", "Holiday X", 2.0, "Snow", 3000),  # Tue, Moderate
        ("2020-01-07 09:00:00", "None", 3.0, "Clear", 5000),   # Tue, Severe
    ]
    df = pd.DataFrame(rows, columns=["date_time", "holiday", "temp_c", "weather_main", "traffic_volume"])
    df["congestion_level"] = ["Low", "High", "Severe", "Moderate", "Moderate", "Severe"]
    return df


def build_zip(overrides: dict | None = None, drop: list[str] | None = None, cleaned: pd.DataFrame | None = None) -> bytes:
    """Build a valid results ZIP, optionally replacing or dropping members."""
    cleaned = small_cleaned() if cleaned is None else cleaned
    files = {
        "traffic_summary.json": json.dumps({
            "total_records": 6, "avg_traffic_volume": 3500.0, "max_traffic_volume": 6000,
            "min_traffic_volume": 1000, "peak_hour": 9, "date_start": "2020-01-06", "date_end": "2020-01-07",
            "source": "unit-test fixture",
        }),
        "hourly_trends.csv": "hour,avg_volume,max_volume,min_volume,record_count\n7,1500,2000,1000,2\n8,3500,4000,3000,2\n9,5500,6000,5000,2\n",
        "daily_trends.csv": "date,avg_volume,max_volume,record_count\n2020-01-06,3666.67,6000,3\n2020-01-07,3333.33,5000,3\n",
        "monthly_trends.csv": "year_month,avg_volume,max_volume,record_count\n2020-01,3500,6000,6\n",
        "congestion_distribution.csv": "congestion_level,record_count,percentage\nLow,1,16.67\nModerate,2,33.33\nHigh,1,16.67\nSevere,2,33.33\n",
        "cleaned_traffic.csv": cleaned.to_csv(index=False),
    }
    files.update(overrides or {})
    for name in drop or []:
        files.pop(name, None)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=tmp_path / "test.db")
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def upload(client, data: bytes, name: str = "results.zip"):
    return client.post("/api/import", files={"file": (name, data, "application/zip")})
