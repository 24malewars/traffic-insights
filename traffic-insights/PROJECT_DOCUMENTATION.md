# Traffic Insights - Project Documentation

## 1. Overview

**Goal:** analyse hourly traffic volume on a highway, find when and how congestion builds up, and present the results in an interactive dashboard.

**Big-data part:** the analysis is done with **Apache Spark (PySpark + Spark SQL)** in Google Colab, because Spark is built to process large datasets in parallel. The dataset used here (about 48,000 hourly rows) is small enough for one machine, but the *same code* scales to much larger data - that is the point of the project.

**Presentation part:** a lightweight web dashboard (FastAPI + vanilla JavaScript + Chart.js) running locally on Windows. Spark's output is moved to the dashboard as one ZIP file, so no live connection between Colab and the laptop is needed.

### Important honesty notes
* **Congestion categories are estimates.** The dataset has only *traffic volume*. Low / Moderate / High / Severe come from fixed thresholds on volume (below). They are not measured speed, delay or travel time.
* **Demo data is synthetic** (randomly generated) and labelled everywhere in the UI and API. The project never fetches or invents live traffic statistics.

## 2. Architecture

```
          Google Colab                                  Your Windows laptop
 +----------------------------------+        +---------------------------------------------+
 | UCI dataset (download)           |        |  Browser  http://127.0.0.1:8000             |
 |   -> PySpark clean + features    |        |   index.html / styles.css / app.js          |
 |   -> Spark SQL aggregations      |  ZIP   |   Chart.js charts, tables, filters          |
 |   -> summary + charts            | -----> |        | fetch() JSON / CSV                 |
 |   -> traffic_insights_results.zip| (manual|        v                                    |
 +----------------------------------+ upload)|  FastAPI  backend/main.py                   |
                                             |   validation - analytics (pandas) - REST    |
                                             |        |                                    |
                                             |        v                                    |
                                             |  SQLite  data/traffic.db  (persistent)      |
                                             +---------------------------------------------+
```

**Data flow**
1. Colab produces six files and zips them.
2. In the dashboard, *Import Spark Results* uploads the ZIP to `POST /api/import`.
3. The backend validates everything **in memory** (nothing is extracted to disk). Only if all checks pass, the cleaned records replace the active dataset (SQLite file swapped atomically).
4. All charts/KPIs are computed from the active dataset by the API, so date filters work on imported data too.

**Why recompute from `cleaned_traffic.csv` instead of displaying the trend CSVs directly?** The date filter needs row-level data. The trend CSVs and summary JSON are still validated and cross-checked (a mismatch produces an import *warning*), and the summary is shown on the Import & Data page.

## 3. Congestion estimate

| Level | Hourly volume (vehicles) |
|---|---|
| Low | < 1500 |
| Moderate | 1500 - 3499 |
| High | 3500 - 4999 |
| Severe | >= 5000 |

The same thresholds are used in the notebook (`LOW_MAX`, `MODERATE_MAX`, `HIGH_MAX`), the backend (`congestion_level()`), and the demo-data generator. They are a project choice, not an official standard; the dataset's maximum is about 7,300 vehicles/hour.

## 4. Data

### 4.1 Source dataset
UCI *Metro Interstate Traffic Volume*: hourly westbound traffic on I-94 between Minneapolis and St Paul, with weather and holiday information, 2012-2018. Columns: `holiday, temp (Kelvin), rain_1h, snow_1h, clouds_all, weather_main, weather_description, date_time, traffic_volume`.

### 4.2 Cleaning steps in the notebook
1. Parse `date_time`; drop rows with missing timestamp or volume.
2. Remove impossible values: temperature <= 0 K, rain > 500 mm/hour, negative volume.
3. Several rows share one timestamp (one per weather description). Keep **one row per hour** (alphabetically first weather label) so each hour's traffic is counted once.
4. Fill missing rain/snow/cloud values with 0 and missing labels with `None`/`Unknown`.
5. `temp_c = temp - 273.15`; add `hour, day_of_week, day_num, month, year, year_month, date, congestion_level`.

The notebook prints a cleaning report (rows before/after each step) and stores it in `traffic_summary.json`.

### 4.3 Files in `traffic_insights_results.zip`

| File | Required columns |
|---|---|
| `traffic_summary.json` | keys: `total_records, avg_traffic_volume, max_traffic_volume, min_traffic_volume, peak_hour, date_start, date_end` (+ optional `source`, `generated_at`, `is_synthetic`, `cleaning_report`, ...) |
| `hourly_trends.csv` | `hour, avg_volume, max_volume, min_volume, record_count` |
| `daily_trends.csv` | `date, avg_volume, max_volume, record_count` |
| `monthly_trends.csv` | `year_month, avg_volume, max_volume, record_count` |
| `congestion_distribution.csv` | `congestion_level, record_count, percentage` |
| `cleaned_traffic.csv` | `date_time, traffic_volume, congestion_level, holiday, temp_c, weather_main` (+ optional `rain_1h, snow_1h, clouds_all, weather_description`; extra columns ignored) |

### 4.4 Storage
SQLite file `data/traffic.db` with table `records` (one row per hour) and table `meta` (one JSON row: dataset kind, source, synthetic flag, import time, Spark summary, warnings). The file is written to a temporary name and swapped in with `os.replace`, so a failed import can never leave half-written data. If the file is missing or corrupt the app falls back to the demo data.

## 5. Backend API

Base URL `http://127.0.0.1:8000`. Interactive docs at `/docs`. All errors are JSON: `{"error": "message", "details": ["..."]}`.

Date filters (`start`, `end`, format `YYYY-MM-DD`, inclusive) are accepted by every analytics/records/export endpoint.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | `{status, version, rows_loaded, dataset_kind, time}` |
| GET | `/api/dataset` | kind (`demo`/`imported`), source, `is_synthetic`, rows, date range, warnings, thresholds, disclaimer |
| GET | `/api/summary` | KPI object: totals, average, median, max/min, peak & quietest hour, busiest weekday/month, % High+Severe, date range |
| GET | `/api/trends/hourly` | `hour, avg_volume, max_volume, min_volume, record_count` |
| GET | `/api/trends/daily` | one row per date |
| GET | `/api/trends/weekday` | Monday..Sunday |
| GET | `/api/trends/monthly` | `year_month` |
| GET | `/api/breakdown/congestion` | counts and percentages for the 4 levels (+ disclaimer) |
| GET | `/api/breakdown/weather` | average volume per `weather_main` |
| GET | `/api/records` | `search, level, sort, order, page, page_size (1-500)`; returns `data, total, page, pages` |
| GET | `/api/export/{name}` | CSV (UTF-8 with BOM for Excel). `name`: `records, hourly, daily, weekday, monthly, congestion, weather` |
| POST | `/api/import` | multipart field `file` = the ZIP. 200 on success; 400/413/422 with error list otherwise |
| POST | `/api/reset` | restore the synthetic demo dataset |

Example (PowerShell):
```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/summary?start=2017-03-01&end=2017-03-31"
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/reset
curl.exe -F "file=@traffic_insights_results.zip" http://127.0.0.1:8000/api/import
```

### 5.1 Import validation (all run before anything is stored)
* upload size <= 50 MB; unpacked size <= 200 MB (zip-bomb guard); at most 200 members; no encrypted ZIPs
* must be a valid ZIP; member paths with `..`, absolute paths or drive letters are rejected (files are never extracted to disk anyway)
* all six files present (a single wrapper folder is allowed); no duplicate copies
* each CSV: UTF-8, parseable, required columns present, not empty
* value checks: hour 0-23, numeric volumes >= 0, `year_month` like `2020-03`, valid dates (`YYYY-MM-DD HH:MM:SS`), congestion level in the allowed list
* JSON: valid object with the required keys and numeric fields
* **all** problems are collected and returned together, so you can fix them in one go
* cross-checks (warnings only): summary `total_records` vs cleaned rows, aggregate `record_count` sums vs cleaned rows, labels that disagree with the volume thresholds
* CSV exports neutralise cells starting with `= + - @` (formula injection)

## 6. Frontend

Plain HTML/CSS/JS - no build step. Chart.js 4.4.1 is copied into `frontend/vendor/` so the dashboard works offline.

| Part | What it does |
|---|---|
| Sidebar + hash routing | `#overview #trends #congestion #records #import`; collapses to a hamburger menu on small screens |
| Date filter | from/to dates + quick ranges (relative to the dataset's last date); applies to every page |
| Overview | 8 KPI cards, hourly line, congestion doughnut, weekday bars, daily line |
| Trends | tabs Hourly / Daily / Monthly: chart + searchable, sortable, paged table + CSV export |
| Congestion | doughnut, average volume by weather, distribution table, threshold explanation |
| Records | server-side search, level filter, sort, paging, CSV export of the current filter |
| Import & Data | drag-and-drop ZIP, client-side checks (type/size), error list, dataset info, reset button |
| States | skeleton KPI cards, per-chart loading / empty / error overlays with Retry, toasts |
| Safety | all text is inserted with `textContent` (no `innerHTML`), so imported data cannot inject HTML |

## 7. Source files

| File | Responsibility |
|---|---|
| `backend/main.py` | constants, pure calculation functions, record validation, ZIP parsing, `Store` (memory + SQLite), `create_app()` with all routes, static file mount |
| `frontend/index.html` | page structure, all five sections |
| `frontend/styles.css` | layout, theme (light/dark), responsive rules, components |
| `frontend/app.js` | API helper, state, chart/table/KPI rendering, filters, import/reset, navigation |
| `colab/traffic_insights.ipynb` | PySpark pipeline: download -> clean -> Spark SQL -> stats -> charts -> ZIP |
| `data/generate_sample_data.py` | creates the synthetic demo CSV and sample ZIP |
| `tests/conftest.py` | fixtures, `build_zip()` helper that makes valid/broken ZIPs |
| `tests/test_calculations.py` | hand-calculated checks of every metric function |
| `tests/test_api.py` | every endpoint, filters, paging, exports, reset, persistence |
| `tests/test_import.py` | valid imports and every invalid-ZIP case |

## 8. Testing

Run: `python -m pytest` (100 tests, about 15 seconds).

* **Calculations:** thresholds at every boundary, summary, hourly/daily/weekday/monthly trends, congestion percentages, weather breakdown, filtering, search/sort, empty data, CSV-injection guard.
* **API:** health, dataset label, summary vs. pandas, date filters, invalid dates, empty ranges, paging/sorting/search, bad parameters, CSV exports, reset, persistence across restart, corrupt DB fallback, frontend files served.
* **Import:** successful import updates all endpoints; folder-wrapped ZIP; BOM/extra columns; warnings; non-ZIP, empty, truncated, missing field; each of the six files missing; duplicates; path traversal; oversize; zip-bomb guard; each missing column; empty CSVs; bad values; bad JSON; failed imports leave the old data untouched.

**Not covered by automated tests:** the real UCI download, chart appearance in a real browser, and Colab itself. During development the notebook code was executed locally with PySpark 3.5.3 against a UCI-format fixture (including duplicate timestamps and bad sensor values), and its output ZIP was imported successfully; the page logic was exercised in a simulated browser (jsdom).

## 9. Limitations

* Congestion is an **estimate from volume** with arbitrary thresholds; it ignores speed, road capacity, incidents and direction.
* One road segment, one direction, 2012-2018; the data has gaps, so averages cover different numbers of hours. Not suitable for forecasting or live decisions.
* The dataset is small; Spark is used to demonstrate the technique (it would be overkill for this size in production).
* Keeping one weather row per hour makes weather labels approximate for hours that had several.
* The dashboard holds one dataset at a time (a new import replaces the old one) and has no user accounts - it is meant for local use, not for exposing to the internet.
* Colab -> dashboard transfer is manual (download ZIP, upload ZIP) by design.

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `python` or `pip` not recognised | Python not on PATH. Reinstall with *Add python.exe to PATH*, or use `py -m venv .venv`, `py -m pip ...` |
| `running scripts is disabled` when activating venv | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` then activate again |
| `ModuleNotFoundError: backend` | run `uvicorn` from the project root folder (where `README.md` is), not from inside `backend\` |
| `ModuleNotFoundError: fastapi` | venv not activated - run `.\.venv\Scripts\Activate.ps1` and `pip install -r requirements.txt` |
| `[Errno 10048]` address already in use | another program uses port 8000: `--port 8001` |
| Browser shows "Cannot reach the server" | backend stopped; restart `uvicorn` |
| Charts empty, banner mentions Chart.js | `frontend\vendor\chart.umd.js` missing - re-extract the ZIP |
| Import says "missing required file(s)" | zip the six files directly (or one folder containing them); use the notebook's own ZIP |
| Import says column/row problems | message names file + column + row numbers; fix the source or re-run the notebook's cleaning/export cells |
| Dashboard shows old data after errors | failed imports never change data; press *Reset to demo data* or delete `data\traffic.db` |
| Colab: download of UCI fails | the notebook asks you to upload the dataset ZIP manually from https://archive.ics.uci.edu/dataset/492 |
| Colab: Java / Spark error on first run | `Runtime -> Restart session`, then *Run all* again |
| Colab: session timed out | re-run all cells; the pipeline is deterministic |

## 11. Viva questions and short answers

1. **Why Spark for ~48k rows?** To show a scalable approach; the same DataFrame/SQL code runs unchanged on a cluster and much larger data. For this size pandas would suffice.
2. **What is a Spark DataFrame vs RDD?** A DataFrame is a distributed table with a schema, optimised by the Catalyst optimiser; an RDD is a lower-level distributed collection without schema.
3. **What are lazy evaluation and actions?** Transformations (`filter`, `withColumn`) only build a plan; actions (`count`, `show`, `toPandas`) trigger execution. I `cache()` the cleaned DataFrame because several actions reuse it.
4. **What is a window function and where did you use it?** Calculations across related rows. `row_number()` over `PARTITION BY date_time` keeps one row per hour; `SUM(COUNT(*)) OVER ()` gives percentages.
5. **Why Spark SQL?** Declarative, readable aggregations (`GROUP BY hour`) running on the same engine as the DataFrame API.
6. **How did you clean the data?** Parsed timestamps, removed impossible sensor values (0 K temperature, absurd hourly rainfall), de-duplicated hours, converted Kelvin to Celsius, filled blanks.
7. **How is congestion defined?** Volume thresholds (Low/Moderate/High/Severe). It is an **estimate** because there is no speed/delay data.
8. **What would improve the estimate?** Speed data, road capacity (volume/capacity ratio), incident data, per-lane counts.
9. **Why not connect the website live to Colab?** Colab sessions are temporary and not publicly addressable; a ZIP hand-over is simpler, offline-friendly and testable.
10. **Why FastAPI?** Fast, automatic validation and `/docs` page, simple Python, async-capable file upload.
11. **How do you keep imports safe?** Size limits, zip-bomb guard, path checks, no extraction to disk, strict validation, atomic DB swap, text escaping in the UI, CSV formula-injection guard.
12. **Why SQLite?** Zero-setup, a single file, persistent across restarts; ideal for one local user.
13. **Why vanilla JS and no framework?** No Node/npm/build tooling on the laptop; fewer moving parts for a beginner; Chart.js is just one script file.
14. **How does the date filter work with imported data?** The API filters the row-level records by date and recomputes every metric, so imported data behaves exactly like the demo data.
15. **How did you test it?** pytest with hand-calculated expectations; API tests via FastAPI `TestClient`; negative tests for every invalid-ZIP case; honest list of what is not covered (real download, real browser rendering).
16. **What does the correlation of temperature and volume show?** Printed in the notebook; correlation is not causation - season and time of day influence both.
17. **What are the limitations?** See section 9.
18. **How would you scale this up?** Run Spark on a cluster (Databricks/EMR), read from cloud storage/Parquet, write aggregates to a database, schedule the job, stream live counts with Structured Streaming.
19. **Why keep `cleaned_traffic.csv` in the ZIP?** Enables record-level search, filtering and date ranges in the dashboard.
20. **What is the difference between the daily and weekday trends?** Daily = one point per calendar date (timeline); weekday = average by day name (Monday-Sunday pattern).

## 12. Credits

Dataset: Hogue, John (2019). *Metro Interstate Traffic Volume*. UCI Machine Learning Repository. https://doi.org/10.24432/C5X60B. Chart.js 4.4.1 (MIT). Apache Spark, FastAPI, pandas.
