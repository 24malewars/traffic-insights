# Traffic Insights - Traffic Congestion Analysis Using Apache Spark

A college project in two parts:

1. **Google Colab notebook** (`colab/traffic_insights.ipynb`) - downloads the UCI *Metro Interstate Traffic Volume* dataset, cleans and analyses it with **PySpark + Spark SQL**, draws charts and exports `traffic_insights_results.zip`.
2. **Web dashboard** (FastAPI + vanilla JS + Chart.js) - runs on your laptop at `http://127.0.0.1:8000`, shows KPIs and charts, and lets you **import the Spark results ZIP**.

> **Congestion levels are estimates.** The dataset has traffic *volume* only (no speed or delay). Low / Moderate / High / Severe come from fixed volume thresholds (Low < 1500, Moderate 1500-3499, High 3500-4999, Severe >= 5000 vehicles/hour).
>
> **Demo data is synthetic.** Before you import anything, the dashboard shows randomly generated sample data and says so in a badge and a banner. No live traffic is ever fetched.

## Quick start (Windows PowerShell)

Requires Python 3.10+ (check with `python --version`; install from python.org and tick "Add python.exe to PATH").

```powershell
cd $HOME\Downloads
Expand-Archive .\traffic-insights.zip -DestinationPath . -Force
cd .\traffic-insights

python -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
python -m pip install --upgrade pip
pip install -r requirements.txt

uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000** in your browser. Stop the server with `Ctrl+C`.

Run the automated tests (in the same activated window):

```powershell
pip install -r requirements-dev.txt
python -m pytest
```

## Using the Colab notebook

1. Go to https://colab.research.google.com -> **File -> Upload notebook** -> choose `colab\traffic_insights.ipynb`.
2. **Runtime -> Run all**. The notebook installs PySpark, downloads the dataset, processes it and shows charts (about 3-6 minutes).
3. The last cell downloads `traffic_insights_results.zip`.
   (If the automatic UCI download is blocked, the notebook asks you to upload the dataset ZIP from https://archive.ics.uci.edu/dataset/492.)

## Importing results into the dashboard

1. Open the dashboard -> **Import & Data** (left sidebar).
2. Drop `traffic_insights_results.zip` on the upload box -> **Import Spark Results**.
3. Every KPI, chart and table now uses your Spark output. The badge changes to *Spark results (imported)*.
4. **Reset to demo data** restores the synthetic sample.

No Colab connection is needed - the ZIP is the hand-over. Want to try the button without running Colab? Use `data\sample_spark_results.zip` (synthetic, labelled as such).

## Project layout

```
traffic-insights/
|-- backend/main.py                FastAPI app: API, validation, SQLite storage, serves the frontend
|-- frontend/                      index.html, styles.css, app.js, vendor/chart.umd.js (Chart.js, bundled -> works offline)
|-- colab/traffic_insights.ipynb   PySpark pipeline for Google Colab
|-- data/                          synthetic demo data + sample ZIP + generator (see data/README.md)
|-- tests/                         pytest suite (calculations, API, imports)
|-- requirements.txt               runtime dependencies (fastapi, uvicorn, pandas, python-multipart)
|-- requirements-dev.txt           + pytest, httpx
|-- README.md, PROJECT_DOCUMENTATION.md, .env.example, .gitignore, pytest.ini
```

## API at a glance

Interactive docs: http://127.0.0.1:8000/docs

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | status, rows loaded |
| GET | `/api/dataset` | active dataset info, thresholds, warnings |
| GET | `/api/summary` | KPIs (`?start=YYYY-MM-DD&end=YYYY-MM-DD`) |
| GET | `/api/trends/{hourly\|daily\|weekday\|monthly}` | trend series |
| GET | `/api/breakdown/{congestion\|weather}` | breakdowns |
| GET | `/api/records` | paged, searchable, sortable records |
| GET | `/api/export/{records\|hourly\|daily\|weekday\|monthly\|congestion\|weather}` | CSV download |
| POST | `/api/import` | upload `traffic_insights_results.zip` |
| POST | `/api/reset` | back to demo data |

## Troubleshooting (short version)

| Problem | Fix |
|---|---|
| `python` not found | Reinstall Python and tick *Add to PATH*, or use `py -m venv .venv` |
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` |
| `Address already in use` / port 8000 busy | use `--port 8001` and open `http://127.0.0.1:8001` |
| Page says "Cannot reach the server" | the server window was closed - start `uvicorn` again |
| Import error list | read each line; it names the file and column. Re-run the notebook's last cells |
| Want a clean start | stop the server and delete `data\traffic.db` |

More in `PROJECT_DOCUMENTATION.md` (architecture, API details, limitations, viva questions).

## What was verified (and what was not)

* Backend: **100 automated tests** (calculations, endpoints, valid/invalid imports, empty data) - all pass.
* Notebook: the notebook's code was executed end-to-end with real PySpark 3.5.3 on a UCI-format test file, and the ZIP it produced imported into the backend without warnings. The sandbox used for development could not reach the UCI website, so the **real dataset download has not been run by the author** - run the notebook in Colab to confirm on real data.
* Frontend: page logic (KPIs, tables, search/sort, date filter, import, reset) was exercised in a simulated browser (jsdom) against the live server. Chart drawing was not visually checked in a real browser.

Dataset: Hogue, John (2019). *Metro Interstate Traffic Volume*. UCI Machine Learning Repository. https://doi.org/10.24432/C5X60B - Chart.js (MIT licence) is bundled in `frontend/vendor/`.
