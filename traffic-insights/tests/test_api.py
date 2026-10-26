"""API endpoint tests (FastAPI TestClient, temporary SQLite database per test)."""
import io
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from conftest import build_zip, small_cleaned, upload

SAMPLE = pd.read_csv(Path(__file__).resolve().parent.parent / "data" / "sample_traffic.csv")


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["rows_loaded"] == len(SAMPLE) and body["dataset_kind"] == "demo"


def test_dataset_is_labelled_synthetic_demo(client):
    r = client.get("/api/dataset").json()
    assert r["kind"] == "demo" and r["is_synthetic"] is True
    assert "SYNTHETIC" in r["source"]
    assert "ESTIMATES" in r["disclaimer"]
    assert r["thresholds"]["Severe"] == ">= 5000"


def test_summary_matches_pandas(client):
    data = client.get("/api/summary").json()["data"]
    assert data["total_records"] == len(SAMPLE)
    assert data["avg_traffic_volume"] == pytest.approx(SAMPLE["traffic_volume"].mean(), abs=0.01)
    assert data["max_traffic_volume"] == SAMPLE["traffic_volume"].max()


def test_summary_date_filter(client):
    sub = SAMPLE[(SAMPLE.date_time >= "2017-03-01") & (SAMPLE.date_time < "2017-04-01")]
    data = client.get("/api/summary", params={"start": "2017-03-01", "end": "2017-03-31"}).json()["data"]
    assert data["total_records"] == len(sub)
    assert data["avg_traffic_volume"] == pytest.approx(sub["traffic_volume"].mean(), abs=0.01)


def test_filter_outside_range_returns_empty_not_error(client):
    r = client.get("/api/summary", params={"start": "1999-01-01", "end": "1999-12-31"})
    assert r.status_code == 200 and r.json()["data"]["empty"] is True
    for path in ["/api/trends/hourly", "/api/trends/daily", "/api/trends/monthly", "/api/trends/weekday", "/api/breakdown/weather"]:
        body = client.get(path, params={"start": "1999-01-01", "end": "1999-12-31"}).json()
        assert body["data"] == [] and body["count"] == 0
    cong = client.get("/api/breakdown/congestion", params={"start": "2100-01-01"}).json()["data"]
    assert [c["record_count"] for c in cong] == [0, 0, 0, 0]
    rec = client.get("/api/records", params={"start": "2100-01-01"}).json()
    assert rec["total"] == 0 and rec["data"] == [] and rec["pages"] == 1


@pytest.mark.parametrize("params", [{"start": "not-a-date"}, {"end": "2017-13-45"}, {"start": "2017-05-02", "end": "2017-05-01"}])
def test_invalid_date_filters_give_400(client, params):
    r = client.get("/api/summary", params=params)
    assert r.status_code == 400 and r.json()["error"]


@pytest.mark.parametrize("kind,key,n", [("hourly", "hour", 24), ("weekday", "day_of_week", 7), ("monthly", "year_month", 12)])
def test_trend_endpoints(client, kind, key, n):
    body = client.get(f"/api/trends/{kind}").json()
    assert body["count"] == n and key in body["data"][0]


def test_daily_trend_has_one_row_per_day(client):
    assert client.get("/api/trends/daily").json()["count"] == SAMPLE["date_time"].str[:10].nunique()


def test_unknown_trend_and_breakdown_404(client):
    assert client.get("/api/trends/yearly").status_code == 404
    assert client.get("/api/breakdown/colour").status_code == 404
    assert client.get("/api/does-not-exist").status_code == 404
    assert "error" in client.get("/api/nope").json()


def test_breakdowns(client):
    cong = client.get("/api/breakdown/congestion").json()
    assert [c["congestion_level"] for c in cong["data"]] == ["Low", "Moderate", "High", "Severe"]
    assert sum(c["record_count"] for c in cong["data"]) == len(SAMPLE)
    assert "ESTIMATES" in cong["disclaimer"]
    assert client.get("/api/breakdown/weather").json()["count"] > 0


def test_records_paging_sorting_search(client):
    r = client.get("/api/records", params={"page_size": 10, "sort": "traffic_volume", "order": "desc"}).json()
    assert r["total"] == len(SAMPLE) and len(r["data"]) == 10 and r["pages"] == -(-len(SAMPLE) // 10)
    vols = [x["traffic_volume"] for x in r["data"]]
    assert vols == sorted(vols, reverse=True) and vols[0] == SAMPLE["traffic_volume"].max()
    p2 = client.get("/api/records", params={"page_size": 10, "page": 2, "sort": "traffic_volume", "order": "desc"}).json()
    assert p2["data"][0] != r["data"][0]
    snow = client.get("/api/records", params={"search": "snow", "page_size": 500}).json()
    assert snow["total"] == int((SAMPLE["weather_main"].str.lower().str.contains("snow") | SAMPLE["weather_description"].str.lower().str.contains("snow") | SAMPLE["holiday"].str.lower().str.contains("snow")).sum())
    sev = client.get("/api/records", params={"level": "Severe", "page_size": 500}).json()
    assert sev["total"] > 0 and all(x["congestion_level"] == "Severe" for x in sev["data"])


@pytest.mark.parametrize("params", [{"sort": "password"}, {"order": "sideways"}, {"level": "Gridlock"}, {"page": 0}, {"page_size": 501}, {"page_size": 0}])
def test_records_bad_parameters(client, params):
    r = client.get("/api/records", params=params)
    assert r.status_code in (400, 422) and r.json()["error"]


def test_search_is_literal_not_regex(client):
    r = client.get("/api/records", params={"search": ".*("})
    assert r.status_code == 200 and r.json()["total"] == 0


def test_export_records_csv(client):
    r = client.get("/api/export/records", params={"start": "2017-01-01", "end": "2017-01-01", "sort": "date_time"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    df = pd.read_csv(io.StringIO(r.text.lstrip("﻿")))
    assert len(df) == 24 and {"date_time", "traffic_volume", "congestion_level"} <= set(df.columns)


@pytest.mark.parametrize("name", ["hourly", "daily", "weekday", "monthly", "congestion", "weather"])
def test_export_aggregates(client, name):
    r = client.get(f"/api/export/{name}")
    assert r.status_code == 200 and len(pd.read_csv(io.StringIO(r.text.lstrip("﻿")))) > 0


def test_export_errors(client):
    assert client.get("/api/export/secrets").status_code == 404
    assert client.get("/api/export/records", params={"sort": "nope"}).status_code == 400


def test_export_blocks_csv_formula_injection(client):
    bad = small_cleaned(); bad.loc[0, "weather_main"] = "=HYPERLINK(\"http://evil\")"
    assert upload(client, build_zip(cleaned=bad)).status_code == 200
    text = client.get("/api/export/records").text
    assert "'=HYPERLINK" in text and ",=HYPERLINK" not in text


def test_reset_restores_demo(client):
    assert upload(client, build_zip()).status_code == 200
    assert client.get("/api/health").json()["rows_loaded"] == 6
    r = client.post("/api/reset")
    assert r.status_code == 200 and r.json()["dataset"]["kind"] == "demo"
    assert client.get("/api/health").json()["rows_loaded"] == len(SAMPLE)


def test_imported_data_survives_restart(tmp_path):
    db = tmp_path / "persist.db"
    with TestClient(create_app(db_path=db)) as c1:
        assert upload(c1, build_zip()).status_code == 200
    with TestClient(create_app(db_path=db)) as c2:        # a "new server process"
        info = c2.get("/api/dataset").json()
        assert info["kind"] == "imported" and info["rows"] == 6


def test_corrupt_database_falls_back_to_demo(tmp_path):
    db = tmp_path / "broken.db"
    db.write_bytes(b"this is not a sqlite database")
    with TestClient(create_app(db_path=db)) as c:
        assert c.get("/api/health").json()["dataset_kind"] == "demo"


def test_frontend_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Traffic Insights" in r.text
    assert client.get("/app.js").status_code == 200 and client.get("/styles.css").status_code == 200
    assert client.get("/vendor/chart.umd.js").status_code == 200


def test_openapi_lists_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    for p in ["/api/health", "/api/summary", "/api/trends/{kind}", "/api/breakdown/{kind}", "/api/records", "/api/import", "/api/reset", "/api/export/{dataset_name}"]:
        assert p in paths
