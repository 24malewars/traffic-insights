"""Import tests: successful ZIP imports and every kind of invalid upload."""
import io
import json
import zipfile
from pathlib import Path

import pytest

import backend.main as backend_main
from conftest import REQUIRED_FILES, build_zip, small_cleaned, upload

SAMPLE_ZIP = Path(__file__).resolve().parent.parent / "data" / "sample_spark_results.zip"


def demo_rows(client):
    return client.get("/api/health").json()["rows_loaded"]


def assert_rejected(client, data, status=400, contains=None):
    """A rejected import must give a clear error and must NOT touch the active dataset."""
    before = client.get("/api/dataset").json()
    r = upload(client, data)
    assert r.status_code == status, r.text
    body = r.json()
    assert body["error"]
    if contains:
        assert contains.lower() in (body["error"] + " " + " ".join(body["details"])).lower(), body
    after = client.get("/api/dataset").json()
    assert (after["kind"], after["rows"], after["source"]) == (before["kind"], before["rows"], before["source"])
    return body


# ---------------------------------------------------------------- success
def test_successful_import_replaces_all_metrics(client):
    r = upload(client, build_zip())
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == 6 and body["warnings"] == []
    assert body["dataset"]["kind"] == "imported" and body["dataset"]["upload_filename"] == "results.zip"

    s = client.get("/api/summary").json()["data"]
    assert (s["total_records"], s["avg_traffic_volume"], s["peak_hour"]) == (6, 3500.0, 9)
    assert client.get("/api/trends/hourly").json()["count"] == 3
    assert client.get("/api/trends/daily").json()["count"] == 2
    assert [c["record_count"] for c in client.get("/api/breakdown/congestion").json()["data"]] == [1, 2, 1, 2]
    assert client.get("/api/records").json()["total"] == 6


def test_import_real_shaped_sample_zip(client):
    r = upload(client, SAMPLE_ZIP.read_bytes(), "sample_spark_results.zip")
    assert r.status_code == 200 and r.json()["warnings"] == []
    ds = r.json()["dataset"]
    assert ds["is_synthetic"] is True and "SYNTHETIC" in ds["source"]


def test_zip_with_files_inside_a_folder_is_accepted(client):
    inner = zipfile.ZipFile(io.BytesIO(build_zip()))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in inner.namelist():
            zf.writestr(f"traffic_insights_results/{n}", inner.read(n))
    assert upload(client, buf.getvalue()).status_code == 200


def test_utf8_bom_and_extra_columns_are_accepted(client):
    df = small_cleaned(); df["extra_col"] = "x"
    data = build_zip(overrides={"cleaned_traffic.csv": "﻿" + df.to_csv(index=False)})
    assert upload(client, data).status_code == 200


def test_inconsistent_totals_import_with_warning(client):
    summary = json.dumps({"total_records": 999, "avg_traffic_volume": 1, "max_traffic_volume": 1,
                          "min_traffic_volume": 1, "peak_hour": 1, "date_start": "a", "date_end": "b"})
    r = upload(client, build_zip(overrides={"traffic_summary.json": summary}))
    assert r.status_code == 200
    assert any("total_records=999" in w for w in r.json()["warnings"])
    assert client.get("/api/dataset").json()["warnings"]


def test_reimport_after_import_replaces_again(client):
    assert upload(client, build_zip()).status_code == 200
    assert upload(client, SAMPLE_ZIP.read_bytes()).status_code == 200
    assert demo_rows(client) > 6


# ---------------------------------------------------------------- invalid archives
def test_not_a_zip(client):
    assert_rejected(client, b"hello, this is just text", contains="not a valid ZIP")


def test_empty_upload(client):
    assert_rejected(client, b"", contains="empty")


def test_truncated_zip(client):
    data = build_zip()
    assert_rejected(client, data[: len(data) // 2])


def test_missing_upload_field(client):
    r = client.post("/api/import")
    assert r.status_code == 422 and r.json()["error"]


@pytest.mark.parametrize("name", REQUIRED_FILES)
def test_each_required_file_missing(client, name):
    body = assert_rejected(client, build_zip(drop=[name]), contains=name)
    assert "missing required file" in body["error"].lower()


def test_zip_with_wrong_files_lists_what_was_found(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "nope")
    body = assert_rejected(client, buf.getvalue(), contains="readme.txt")
    assert "hourly_trends.csv" in body["error"]


def test_duplicate_required_file(client):
    inner = zipfile.ZipFile(io.BytesIO(build_zip()))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in inner.namelist():
            zf.writestr(f"a/{n}", inner.read(n))
        zf.writestr("b/hourly_trends.csv", inner.read("hourly_trends.csv"))
    assert_rejected(client, buf.getvalue(), contains="duplicate")


def test_path_traversal_member_is_rejected(client):
    inner = zipfile.ZipFile(io.BytesIO(build_zip()))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in inner.namelist():
            zf.writestr(n, inner.read(n))
        zf.writestr("../../evil.csv", "boom")
    assert_rejected(client, buf.getvalue(), contains="unsafe")


def test_oversized_upload_rejected(client, monkeypatch):
    monkeypatch.setattr(backend_main, "MAX_UPLOAD_MB", 1)
    r = upload(client, b"0" * (1024 * 1024 + 10))
    assert r.status_code == 413 and "upload limit" in r.json()["error"]


def test_zip_bomb_guard(client, monkeypatch):
    monkeypatch.setattr(backend_main, "MAX_UNCOMPRESSED_MB", 1)
    big = small_cleaned().assign(note="x" * 200)
    data = build_zip(overrides={"cleaned_traffic.csv": big.to_csv(index=False) + ("a" * 1024 + "\n") * 2000})
    assert_rejected(client, data, status=413, contains="unpack")


# ---------------------------------------------------------------- missing columns
CSV_COLUMN_CASES = [
    ("hourly_trends.csv", "avg_volume"), ("hourly_trends.csv", "hour"),
    ("daily_trends.csv", "date"), ("monthly_trends.csv", "year_month"),
    ("congestion_distribution.csv", "percentage"),
    ("cleaned_traffic.csv", "traffic_volume"), ("cleaned_traffic.csv", "date_time"),
    ("cleaned_traffic.csv", "congestion_level"), ("cleaned_traffic.csv", "weather_main"),
]


@pytest.mark.parametrize("file,column", CSV_COLUMN_CASES)
def test_missing_column(client, file, column):
    import pandas as pd
    original = zipfile.ZipFile(io.BytesIO(build_zip())).read(file).decode()
    df = pd.read_csv(io.StringIO(original)).drop(columns=[column])
    body = assert_rejected(client, build_zip(overrides={file: df.to_csv(index=False)}), contains=column)
    assert file in " ".join(body["details"])


def test_all_problems_are_reported_together(client):
    data = build_zip(overrides={"hourly_trends.csv": "hour\n1\n", "daily_trends.csv": "date\n2020-01-01\n"})
    body = assert_rejected(client, data)
    joined = " ".join(body["details"])
    assert "hourly_trends.csv" in joined and "daily_trends.csv" in joined


# ---------------------------------------------------------------- empty / invalid data
def test_empty_cleaned_data(client):
    header_only = ",".join(small_cleaned().columns) + "\n"
    assert_rejected(client, build_zip(overrides={"cleaned_traffic.csv": header_only}), contains="no data rows")


@pytest.mark.parametrize("name,content", [
    ("hourly_trends.csv", "hour,avg_volume,max_volume,min_volume,record_count\n"),
    ("daily_trends.csv", "date,avg_volume,max_volume,record_count\n"),
    ("monthly_trends.csv", "year_month,avg_volume,max_volume,record_count\n"),
    ("congestion_distribution.csv", "congestion_level,record_count,percentage\n"),
])
def test_empty_aggregate_files(client, name, content):
    assert_rejected(client, build_zip(overrides={name: content}), contains="no data rows")


def test_completely_empty_csv(client):
    assert_rejected(client, build_zip(overrides={"hourly_trends.csv": ""}), contains="hourly_trends.csv")


def test_invalid_values_in_cleaned_data(client):
    bad = small_cleaned()
    bad.loc[0, "date_time"] = "yesterday"
    bad.loc[1, "traffic_volume"] = -5
    bad.loc[2, "congestion_level"] = "Gridlock"
    body = assert_rejected(client, build_zip(cleaned=bad))
    joined = " ".join(body["details"])
    assert "date_time" in joined and "traffic_volume" in joined and "congestion_level" in joined


def test_text_in_numeric_aggregate(client):
    bad = "hour,avg_volume,max_volume,min_volume,record_count\n25,abc,1,1,1\n"
    body = assert_rejected(client, build_zip(overrides={"hourly_trends.csv": bad}))
    assert "hour" in " ".join(body["details"]) and "avg_volume" in " ".join(body["details"])


def test_bad_month_format(client):
    bad = "year_month,avg_volume,max_volume,record_count\n2020/01,1,1,1\n"
    assert_rejected(client, build_zip(overrides={"monthly_trends.csv": bad}), contains="year_month")


@pytest.mark.parametrize("content,expected", [
    ("{not json", "not valid JSON"),
    ("[1, 2, 3]", "must contain a JSON object"),
    ("{}", "missing key"),
    (json.dumps({"total_records": "six", "avg_traffic_volume": 1, "max_traffic_volume": 1, "min_traffic_volume": 1,
                 "peak_hour": 1, "date_start": "a", "date_end": "b"}), "must be a number"),
])
def test_invalid_summary_json(client, content, expected):
    assert_rejected(client, build_zip(overrides={"traffic_summary.json": content}), contains=expected)


def test_failed_import_keeps_previous_imported_data(client):
    assert upload(client, build_zip()).status_code == 200
    assert_rejected(client, build_zip(drop=["cleaned_traffic.csv"]))
    assert client.get("/api/summary").json()["data"]["total_records"] == 6
