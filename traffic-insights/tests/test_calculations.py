"""Unit tests for the calculation helpers (no HTTP involved). Expected values are worked out by hand
from the 6-row dataset in conftest.small_cleaned()."""
import pandas as pd
import pytest

from backend.main import (
    ApiError, compute_summary, congestion_distribution, congestion_level, csv_safe, daily_trend,
    filter_dates, hourly_trend, monthly_trend, prepare_records, query_records, weather_breakdown, weekday_trend,
)
from conftest import small_cleaned


@pytest.fixture()
def df():
    return prepare_records(small_cleaned())


@pytest.mark.parametrize("volume,expected", [
    (0, "Low"), (1499, "Low"), (1500, "Moderate"), (3499, "Moderate"),
    (3500, "High"), (4999, "High"), (5000, "Severe"), (7280, "Severe"),
])
def test_congestion_thresholds(volume, expected):
    assert congestion_level(volume) == expected


def test_prepare_records_derives_time_fields(df):
    first = df.iloc[0]
    assert first["date"] == "2020-01-06"
    assert first["hour"] == 7
    assert first["day_of_week"] == "Monday"
    assert first["year_month"] == "2020-01"
    assert len(df) == 6


def test_summary_values(df):
    s = compute_summary(df)
    assert s["empty"] is False
    assert s["total_records"] == 6
    assert s["avg_traffic_volume"] == 3500.0
    assert s["median_traffic_volume"] == 3500.0
    assert s["max_traffic_volume"] == 6000
    assert s["min_traffic_volume"] == 1000
    assert s["peak_hour"] == 9
    assert s["quietest_hour"] == 7
    assert s["busiest_day_of_week"] == "Monday"      # (1000+4000+6000)/3 > (2000+3000+5000)/3
    assert s["busiest_month"] == "2020-01"
    assert s["high_severe_pct"] == 50.0               # 1 High + 2 Severe out of 6
    assert s["max_volume_at"] == "2020-01-06 09:00:00"
    assert (s["date_start"], s["date_end"], s["days_covered"]) == ("2020-01-06", "2020-01-07", 2)


def test_hourly_trend(df):
    h = hourly_trend(df).set_index("hour")
    assert h.loc[7, "avg_volume"] == 1500 and h.loc[8, "avg_volume"] == 3500 and h.loc[9, "avg_volume"] == 5500
    assert h.loc[9, "max_volume"] == 6000 and h.loc[9, "min_volume"] == 5000
    assert (h["record_count"] == 2).all()


def test_daily_weekday_monthly(df):
    d = daily_trend(df).set_index("date")
    assert d.loc["2020-01-06", "avg_volume"] == pytest.approx(3666.67)
    assert d.loc["2020-01-07", "avg_volume"] == pytest.approx(3333.33)
    w = weekday_trend(df)
    assert list(w["day_of_week"]) == ["Monday", "Tuesday"]           # Monday-first ordering
    m = monthly_trend(df)
    assert list(m["year_month"]) == ["2020-01"] and m.iloc[0]["avg_volume"] == 3500


def test_congestion_distribution(df):
    c = congestion_distribution(df).set_index("congestion_level")
    assert list(c.index) == ["Low", "Moderate", "High", "Severe"]
    assert list(c["record_count"]) == [1, 2, 1, 2]
    assert list(c["percentage"]) == [16.67, 33.33, 16.67, 33.33]
    assert c["percentage"].sum() == pytest.approx(100, abs=0.02)


def test_weather_breakdown(df):
    w = weather_breakdown(df).set_index("weather_main")
    assert w.loc["Clear", "record_count"] == 4 and w.loc["Clear", "avg_volume"] == 3500  # (1000+6000+2000+5000)/4
    assert w.loc["Rain", "avg_volume"] == 4000 and w.loc["Snow", "avg_volume"] == 3000


def test_filter_dates(df):
    assert len(filter_dates(df, "2020-01-07", None)) == 3
    assert len(filter_dates(df, None, "2020-01-06")) == 3
    assert len(filter_dates(df, "2020-01-06", "2020-01-07")) == 6
    assert len(filter_dates(df, "2021-01-01", None)) == 0


def test_query_records_search_level_sort(df):
    assert len(query_records(df, search="snow")) == 1
    assert len(query_records(df, search="holiday x")) == 1
    assert len(query_records(df, level="Severe")) == 2
    top = query_records(df, sort="traffic_volume", order="desc").iloc[0]
    assert top["traffic_volume"] == 6000
    by_level = query_records(df, sort="congestion_level", order="desc").iloc[0]
    assert by_level["congestion_level"] == "Severe"           # sorted by severity, not alphabetically


def test_empty_data_calculations(df):
    empty = df.iloc[0:0]
    s = compute_summary(empty)
    assert s["empty"] is True and s["total_records"] == 0 and s["avg_traffic_volume"] is None
    assert hourly_trend(empty).empty and daily_trend(empty).empty and monthly_trend(empty).empty
    assert weekday_trend(empty).empty and weather_breakdown(empty).empty
    c = congestion_distribution(empty)
    assert list(c["record_count"]) == [0, 0, 0, 0] and list(c["percentage"]) == [0, 0, 0, 0]


def test_prepare_records_rejects_bad_input():
    bad_cols = small_cleaned().drop(columns=["weather_main"])
    with pytest.raises(ApiError, match="missing required column"):
        prepare_records(bad_cols)
    with pytest.raises(ApiError, match="no data rows"):
        prepare_records(small_cleaned().iloc[0:0])
    bad_vol = small_cleaned().astype(str); bad_vol.loc[0, "traffic_volume"] = "abc"
    with pytest.raises(ApiError) as e:
        prepare_records(bad_vol)
    assert "traffic_volume" in " ".join(e.value.details)
    bad_level = small_cleaned().astype(str); bad_level.loc[1, "congestion_level"] = "Gridlock"
    with pytest.raises(ApiError) as e:
        prepare_records(bad_level)
    assert "congestion_level" in " ".join(e.value.details)


def test_csv_safe_blocks_formula_injection():
    out = csv_safe(pd.DataFrame({"a": ["=SUM(A1)", "+1", "-1", "@x", "normal"], "n": [1, -2, 3, 4, 5]}))
    assert list(out["a"]) == ["'=SUM(A1)", "'+1", "'-1", "'@x", "normal"]
    assert list(out["n"]) == [1, -2, 3, 4, 5]                 # numbers are untouched
