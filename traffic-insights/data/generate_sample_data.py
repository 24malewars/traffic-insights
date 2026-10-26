"""
Generate the SYNTHETIC demo dataset used by the dashboard before you import real Spark results.

!!! IMPORTANT !!!
The numbers produced here are RANDOMLY GENERATED to look like hourly highway traffic.
They are NOT real measurements. The dashboard labels this data as "Demo data (synthetic)".

Usage (from the project root):
    python data/generate_sample_data.py

Output:
    data/sample_traffic.csv          (the demo dataset, same columns as Spark's cleaned_traffic.csv)
    data/sample_spark_results.zip    (a ZIP in the same format the Colab notebook produces,
                                      so you can try "Import Spark Results" without running Colab.
                                      It is also synthetic and labelled as such.)
"""
import io
import json
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent

# Same thresholds as the notebook and backend (estimates based on volume only).
THRESHOLDS = {"Low": (0, 1500), "Moderate": (1500, 3500), "High": (3500, 5000), "Severe": (5000, None)}
LEVEL_ORDER = ["Low", "Moderate", "High", "Severe"]


def congestion_level(volume: float) -> str:
    if volume < 1500:
        return "Low"
    if volume < 3500:
        return "Moderate"
    if volume < 5000:
        return "High"
    return "Severe"


HOLIDAYS = {
    "2017-01-01": "New Years Day",
    "2017-05-29": "Memorial Day",
    "2017-07-04": "Independence Day",
    "2017-09-04": "Labor Day",
    "2017-11-23": "Thanksgiving Day",
    "2017-12-25": "Christmas Day",
}
WEATHER = ["Clear", "Clouds", "Rain", "Mist", "Snow", "Fog", "Drizzle"]


def build_dataframe(seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2017-01-01 00:00:00", "2017-12-31 23:00:00", freq="h")
    hour = ts.hour.to_numpy()
    dow = ts.dayofweek.to_numpy()  # Monday = 0
    month = ts.month.to_numpy()
    doy = ts.dayofyear.to_numpy()

    # Weekday: two rush-hour peaks. Weekend: one broad midday hump.
    weekday_profile = (
        900
        + 3600 * np.exp(-((hour - 7.5) ** 2) / 4.5)
        + 4200 * np.exp(-((hour - 16.5) ** 2) / 5.5)
        + 1800 * np.exp(-((hour - 12) ** 2) / 18)
    )
    weekend_profile = 500 + 3300 * np.exp(-((hour - 14) ** 2) / 20)
    base = np.where(dow >= 5, weekend_profile, weekday_profile)
    season = 1 + 0.08 * np.sin((doy - 80) / 365 * 2 * np.pi)
    volume = base * season * rng.normal(1.0, 0.12, len(ts))

    holiday = np.array([HOLIDAYS.get(d.strftime("%Y-%m-%d"), "None") for d in ts], dtype=object)
    # Only label the first hour of a holiday day like the real dataset does.
    holiday = np.where((holiday != "None") & (hour != 0), "None", holiday)
    is_holiday_day = np.array([d.strftime("%Y-%m-%d") in HOLIDAYS for d in ts])
    volume = np.where(is_holiday_day, volume * 0.55, volume)

    weather = rng.choice(WEATHER, len(ts), p=[0.34, 0.30, 0.14, 0.08, 0.06, 0.04, 0.04])
    volume = np.where(np.isin(weather, ["Snow", "Fog"]), volume * 0.9, volume)
    volume = np.clip(np.rint(volume), 0, 7280).astype(int)

    temp_c = 11 + 15 * np.sin((doy - 110) / 365 * 2 * np.pi) + rng.normal(0, 3, len(ts)) + 2 * np.sin((hour - 9) / 24 * 2 * np.pi)
    rain = np.where(np.isin(weather, ["Rain", "Drizzle"]), np.round(rng.gamma(1.2, 0.8, len(ts)), 2), 0.0)
    snow = np.where(weather == "Snow", np.round(rng.gamma(1.1, 0.05, len(ts)), 2), 0.0)
    clouds = np.where(weather == "Clear", rng.integers(0, 20, len(ts)), rng.integers(40, 101, len(ts)))

    df = pd.DataFrame(
        {
            "date_time": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "holiday": holiday,
            "temp_c": np.round(temp_c, 2),
            "rain_1h": rain,
            "snow_1h": snow,
            "clouds_all": clouds,
            "weather_main": weather,
            "weather_description": weather,
            "traffic_volume": volume,
            "hour": hour,
            "day_of_week": ts.day_name().to_numpy(),
            "month": month,
            "year": ts.year.to_numpy(),
        }
    )
    df["congestion_level"] = [congestion_level(v) for v in df["traffic_volume"]]
    return df


def build_results_zip_bytes(df: pd.DataFrame, label: str) -> bytes:
    """Create a ZIP laid out exactly like the Colab notebook's traffic_insights_results.zip."""
    d = df.copy()
    d["ts"] = pd.to_datetime(d["date_time"])
    d["date"] = d["ts"].dt.strftime("%Y-%m-%d")
    hourly = d.groupby("hour")["traffic_volume"].agg(avg_volume="mean", max_volume="max", min_volume="min", record_count="count").round(2).reset_index()
    daily = d.groupby("date")["traffic_volume"].agg(avg_volume="mean", max_volume="max", record_count="count").round(2).reset_index()
    d["year_month"] = d["ts"].dt.strftime("%Y-%m")
    monthly = d.groupby("year_month")["traffic_volume"].agg(avg_volume="mean", max_volume="max", record_count="count").round(2).reset_index()
    cong = d["congestion_level"].value_counts().reindex(LEVEL_ORDER).fillna(0).astype(int).rename_axis("congestion_level").reset_index(name="record_count")
    cong["percentage"] = (cong["record_count"] / cong["record_count"].sum() * 100).round(2)
    summary = {
        "total_records": int(len(d)),
        "avg_traffic_volume": round(float(d["traffic_volume"].mean()), 2),
        "max_traffic_volume": int(d["traffic_volume"].max()),
        "min_traffic_volume": int(d["traffic_volume"].min()),
        "peak_hour": int(hourly.loc[hourly["avg_volume"].idxmax(), "hour"]),
        "quietest_hour": int(hourly.loc[hourly["avg_volume"].idxmin(), "hour"]),
        "date_start": d["date"].min(),
        "date_end": d["date"].max(),
        "congestion_thresholds": {"Low": "< 1500", "Moderate": "1500-3499", "High": "3500-4999", "Severe": ">= 5000"},
        "source": label,
        "is_synthetic": True,
        "notes": "Congestion levels are ESTIMATES derived from traffic volume only.",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("traffic_summary.json", json.dumps(summary, indent=2))
        zf.writestr("hourly_trends.csv", hourly.to_csv(index=False))
        zf.writestr("daily_trends.csv", daily.to_csv(index=False))
        zf.writestr("monthly_trends.csv", monthly.to_csv(index=False))
        zf.writestr("congestion_distribution.csv", cong.to_csv(index=False))
        zf.writestr("cleaned_traffic.csv", df.to_csv(index=False))
    return buf.getvalue()


def main() -> None:
    df = build_dataframe()
    csv_path = DATA_DIR / "sample_traffic.csv"
    df.to_csv(csv_path, index=False)
    zip_path = DATA_DIR / "sample_spark_results.zip"
    zip_path.write_bytes(build_results_zip_bytes(df, "SYNTHETIC SAMPLE - generated by data/generate_sample_data.py, NOT real Spark output"))
    print(f"Wrote {csv_path} ({len(df)} rows) and {zip_path}")


if __name__ == "__main__":
    main()
