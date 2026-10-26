/* Traffic Insights dashboard - vanilla JavaScript + Chart.js (no build step). */
(() => {
  'use strict';

  // ---------- tiny helpers ----------
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const nf = new Intl.NumberFormat('en-US');
  const LEVELS = ['Low', 'Moderate', 'High', 'Severe'];
  const PAGES = { overview: 'Overview', trends: 'Trends', congestion: 'Congestion', records: 'Records', import: 'Import & Data' };
  const MAX_UPLOAD_MB = 50;

  // Create a DOM element safely (text is never parsed as HTML, so imported data cannot inject markup).
  function el(tag, props = {}, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
      else if (v === true) n.setAttribute(k, '');
      else if (v !== false && v != null) n.setAttribute(k, v);
    }
    for (const kid of kids.flat()) {
      if (kid == null) continue;
      n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return n;
  }
  const fmt = (v, d = 0) => (v == null || Number.isNaN(v) ? '—' : Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }));
  const hourLabel = (h) => `${String(h).padStart(2, '0')}:00`;
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const qs = (params) => {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== '' && v != null) p.set(k, v);
    const s = p.toString();
    return s ? `?${s}` : '';
  };

  async function api(path, options) {
    let res;
    try {
      res = await fetch(path, options);
    } catch (e) {
      throw new Error('Cannot reach the server. Is the backend running at http://127.0.0.1:8000 ?');
    }
    let body = null;
    if ((res.headers.get('content-type') || '').includes('json')) body = await res.json().catch(() => null);
    if (!res.ok) {
      const err = new Error((body && body.error) || `Request failed (${res.status})`);
      err.details = (body && body.details) || [];
      throw err;
    }
    return body;
  }

  function toast(message, type = 'info') {
    const t = el('div', { class: `toast ${type}`, role: 'status', text: message });
    $('#toasts').append(t);
    setTimeout(() => t.remove(), type === 'error' ? 7000 : 3500);
  }

  // ---------- state ----------
  const state = {
    page: 'overview',
    filters: { start: '', end: '' },
    dataset: null,
    data: {},      // successful responses by key
    errors: {},    // error message by key
    loading: false,
    trend: 'hourly',
    charts: {},
    records: { page: 1, pageSize: 25, sort: 'date_time', order: 'asc', search: '', level: '', total: 0, pages: 1, rows: [], error: null, loading: false },
  };
  let loadSeq = 0;
  let recSeq = 0;

  const ENDPOINTS = {
    summary: '/api/summary', hourly: '/api/trends/hourly', daily: '/api/trends/daily', weekday: '/api/trends/weekday',
    monthly: '/api/trends/monthly', congestion: '/api/breakdown/congestion', weather: '/api/breakdown/weather',
  };

  // ---------- chart boxes: loading / error / empty overlays ----------
  function setBox(key, status, message) {
    for (const box of $$(`.chart-box[data-chart="${key}"]`)) {
      const old = $('.overlay', box);
      if (old) old.remove();
      if (status === 'ok') continue;
      let content;
      if (status === 'loading') content = [el('div', { class: 'spinner' }), el('span', { text: 'Loading...' })];
      else if (status === 'empty') content = [el('strong', { text: 'No data for this selection' }), el('span', { text: 'Try a wider date range or reset the filters.' })];
      else content = [el('strong', { text: 'Could not load this chart' }), el('span', { text: message || 'Unknown error' }), el('button', { class: 'btn', type: 'button', text: 'Retry', onclick: loadAll })];
      box.append(el('div', { class: `overlay ${status === 'error' ? 'error' : ''}` }, content));
    }
  }

  function themeChartDefaults() {
    Chart.defaults.color = cssVar('--muted');
    Chart.defaults.borderColor = cssVar('--border');
    Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
  }

  function drawChart(canvasId, config) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    if (state.charts[canvasId]) state.charts[canvasId].destroy();
    state.charts[canvasId] = new Chart(canvas, config);
  }

  const levelColors = () => [cssVar('--low'), cssVar('--moderate'), cssVar('--high'), cssVar('--severe')];
  const axisTitle = (text) => ({ display: true, text });
  const baseOptions = (yTitle = 'Vehicles per hour') => ({
    responsive: true, maintainAspectRatio: false,
    interaction: { mode: 'index', intersect: false },
    plugins: { legend: { display: false } },
    scales: { x: { grid: { display: false } }, y: { beginAtZero: true, title: axisTitle(yTitle) } },
  });

  // ---------- chart renderers ----------
  function chartHourly(canvasId, withMax = false) {
    const rows = state.data.hourly || [];
    const datasets = [{ label: 'Average volume', data: rows.map((r) => r.avg_volume), borderColor: cssVar('--primary'), backgroundColor: cssVar('--primary') + '33', fill: true, tension: 0.3, pointRadius: 3 }];
    if (withMax) datasets.push({ label: 'Maximum volume', data: rows.map((r) => r.max_volume), borderColor: cssVar('--high'), borderDash: [6, 4], fill: false, tension: 0.3, pointRadius: 0 });
    const opts = baseOptions();
    opts.plugins.legend.display = withMax;
    drawChart(canvasId, { type: 'line', data: { labels: rows.map((r) => hourLabel(r.hour)), datasets }, options: opts });
  }

  function chartCongestion(canvasId) {
    const rows = state.data.congestion || [];
    drawChart(canvasId, {
      type: 'doughnut',
      data: { labels: rows.map((r) => r.congestion_level), datasets: [{ data: rows.map((r) => r.record_count), backgroundColor: levelColors(), borderColor: cssVar('--surface'), borderWidth: 2 }] },
      options: {
        responsive: true, maintainAspectRatio: false, cutout: '58%',
        plugins: {
          legend: { position: 'bottom' },
          tooltip: { callbacks: { label: (c) => ` ${c.label}: ${nf.format(c.parsed)} hours (${rows[c.dataIndex].percentage}%)` } },
        },
      },
    });
  }

  function chartWeekday(canvasId) {
    const rows = state.data.weekday || [];
    drawChart(canvasId, { type: 'bar', data: { labels: rows.map((r) => r.day_of_week), datasets: [{ label: 'Average volume', data: rows.map((r) => r.avg_volume), backgroundColor: cssVar('--primary'), borderRadius: 6 }] }, options: baseOptions() });
  }

  function chartDaily(canvasId, withMax = false) {
    const rows = state.data.daily || [];
    const datasets = [{ label: 'Average volume', data: rows.map((r) => r.avg_volume), borderColor: cssVar('--primary'), backgroundColor: cssVar('--primary') + '22', fill: true, tension: 0.2, pointRadius: 0, borderWidth: 1.5 }];
    if (withMax) datasets.push({ label: 'Maximum volume', data: rows.map((r) => r.max_volume), borderColor: cssVar('--high'), fill: false, tension: 0.2, pointRadius: 0, borderWidth: 1 });
    const opts = baseOptions();
    opts.plugins.legend.display = withMax;
    opts.scales.x.ticks = { maxTicksLimit: 10, maxRotation: 0 };
    drawChart(canvasId, { type: 'line', data: { labels: rows.map((r) => r.date), datasets }, options: opts });
  }

  function chartMonthly(canvasId) {
    const rows = state.data.monthly || [];
    const opts = baseOptions();
    opts.plugins.legend.display = true;
    drawChart(canvasId, {
      type: 'bar',
      data: {
        labels: rows.map((r) => r.year_month),
        datasets: [
          { type: 'bar', label: 'Average volume', data: rows.map((r) => r.avg_volume), backgroundColor: cssVar('--primary'), borderRadius: 6 },
          { type: 'line', label: 'Maximum volume', data: rows.map((r) => r.max_volume), borderColor: cssVar('--high'), pointRadius: 2, tension: 0.25 },
        ],
      },
      options: opts,
    });
  }

  function chartWeather(canvasId) {
    const rows = state.data.weather || [];
    const opts = baseOptions();
    opts.indexAxis = 'y';
    opts.scales = { x: { beginAtZero: true, title: axisTitle('Average vehicles per hour') }, y: { grid: { display: false } } };
    drawChart(canvasId, { type: 'bar', data: { labels: rows.map((r) => r.weather_main), datasets: [{ label: 'Average volume', data: rows.map((r) => r.avg_volume), backgroundColor: cssVar('--primary'), borderRadius: 6 }] }, options: opts });
  }

  function chartTrend() {
    if (state.trend === 'hourly') chartHourly('chartTrend', true);
    else if (state.trend === 'daily') chartDaily('chartTrend', true);
    else chartMonthly('chartTrend');
  }

  // ---------- KPI cards ----------
  const KPI_DEFS = [
    ['Hourly records', (s) => fmt(s.total_records), (s) => `${fmt(s.days_covered)} days covered`],
    ['Average traffic', (s) => fmt(s.avg_traffic_volume), (s) => `median ${fmt(s.median_traffic_volume)} vehicles/h`],
    ['Peak hour', (s) => (s.peak_hour == null ? '—' : hourLabel(s.peak_hour)), () => 'highest average volume'],
    ['Highest hourly volume', (s) => fmt(s.max_traffic_volume), (s) => s.max_volume_at || ''],
    ['Busiest weekday', (s) => s.busiest_day_of_week || '—', () => 'highest average volume'],
    ['Busiest month', (s) => s.busiest_month || '—', () => 'highest average volume'],
    ['High / Severe hours', (s) => (s.high_severe_pct == null ? '—' : `${fmt(s.high_severe_pct, 1)}%`), () => 'estimated from volume'],
    ['Quietest hour', (s) => (s.quietest_hour == null ? '—' : hourLabel(s.quietest_hour)), (s) => `min volume ${fmt(s.min_traffic_volume)}`],
  ];

  function renderKpis() {
    const grid = $('#kpiGrid');
    grid.replaceChildren();
    if (state.errors.summary) {
      grid.append(el('div', { class: 'card', style: 'grid-column:1/-1' }, el('strong', { text: 'Could not load the key numbers. ' }), state.errors.summary, ' ', el('button', { class: 'btn', type: 'button', text: 'Retry', onclick: loadAll })));
      return;
    }
    const s = state.data.summary;
    for (const [label, valueFn, subFn] of KPI_DEFS) {
      const loading = !s;
      grid.append(el('div', { class: `kpi ${loading ? 'skeleton' : ''}` },
        el('div', { class: 'label', text: label }),
        el('div', { class: 'value', text: loading ? '0000' : valueFn(s) }),
        el('div', { class: 'sub', text: loading ? 'loading' : subFn(s) || ' ' })));
    }
  }

  // ---------- tables ----------
  function cellContent(col, row) {
    const v = row[col.key];
    if (col.render) return col.render(v, row);
    if (col.fmt) return col.fmt(v, row);
    return v == null ? '—' : String(v);
  }

  function levelTag(v) {
    return LEVELS.includes(v) ? el('span', { class: `tag ${v}`, text: v }) : String(v ?? '—');
  }

  function buildTable(columns, rows, sort, onSort, emptyText) {
    const head = el('tr');
    for (const col of columns) {
      const active = sort.key === col.key;
      const arrow = el('span', { class: 'arrow', 'aria-hidden': 'true', text: active ? (sort.dir === 'asc' ? '▲' : '▼') : '⇅' });
      head.append(el('th', { class: col.num ? 'num' : '', scope: 'col', 'aria-sort': active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none' },
        el('button', { type: 'button', title: `Sort by ${col.label}`, onclick: () => onSort(col.key) }, col.label, arrow)));
    }
    const body = el('tbody');
    for (const row of rows) {
      body.append(el('tr', {}, columns.map((col) => el('td', { class: col.num ? 'num' : '' }, cellContent(col, row)))));
    }
    const table = el('table', {}, el('thead', {}, head), body);
    const wrap = el('div', { class: 'table-wrap' }, table);
    if (!rows.length) wrap.append(el('div', { class: 'empty', text: emptyText || 'No rows to show.' }));
    return wrap;
  }

  // Client-side table: search + sort + pagination over rows already in the browser.
  function createClientTable(root, columns, { pageSize = 10, defaultSort = null } = {}) {
    const t = { rows: [], q: '', sort: defaultSort || { key: columns[0].key, dir: 'asc' }, page: 1 };
    const search = el('input', { type: 'search', placeholder: 'Search this table...', 'aria-label': 'Search table' });
    const info = el('span', { class: 'muted' });
    const holder = el('div');
    const pager = el('div', { class: 'pager' });
    root.replaceChildren(el('div', { class: 'table-tools' }, search, info), holder, pager);

    function view() {
      const q = t.q.trim().toLowerCase();
      let rows = t.rows;
      if (q) rows = rows.filter((r) => columns.some((c) => `${r[c.key]} ${typeof cellContent(c, r) === 'string' ? cellContent(c, r) : ''}`.toLowerCase().includes(q)));
      const { key, dir } = t.sort;
      rows = [...rows].sort((a, b) => {
        const x = a[key], y = b[key];
        const cmp = typeof x === 'number' && typeof y === 'number' ? x - y : String(x ?? '').localeCompare(String(y ?? ''), undefined, { numeric: true });
        return dir === 'asc' ? cmp : -cmp;
      });
      return rows;
    }

    function render() {
      const rows = view();
      const pages = Math.max(1, Math.ceil(rows.length / pageSize));
      t.page = Math.min(t.page, pages);
      const slice = rows.slice((t.page - 1) * pageSize, t.page * pageSize);
      info.textContent = `${nf.format(rows.length)} row${rows.length === 1 ? '' : 's'}`;
      holder.replaceChildren(buildTable(columns, slice, t.sort, (key) => {
        t.sort = { key, dir: t.sort.key === key && t.sort.dir === 'asc' ? 'desc' : 'asc' };
        render();
      }, t.q ? 'No rows match your search.' : 'No data for this date range.'));
      pager.replaceChildren(
        el('button', { class: 'btn', type: 'button', text: 'Previous', disabled: t.page <= 1, onclick: () => { t.page--; render(); } }),
        el('span', { class: 'muted', text: `Page ${t.page} of ${pages}` }),
        el('button', { class: 'btn', type: 'button', text: 'Next', disabled: t.page >= pages, onclick: () => { t.page++; render(); } }));
    }
    search.addEventListener('input', () => { t.q = search.value; t.page = 1; render(); });
    return { setRows(rows) { t.rows = rows || []; t.page = 1; render(); } };
  }

  const TREND_COLUMNS = (first) => [
    first,
    { key: 'avg_volume', label: 'Avg volume', num: true, fmt: (v) => fmt(v, 1) },
    { key: 'max_volume', label: 'Max volume', num: true, fmt: (v) => fmt(v) },
    { key: 'min_volume', label: 'Min volume', num: true, fmt: (v) => fmt(v) },
    { key: 'record_count', label: 'Records', num: true, fmt: (v) => fmt(v) },
  ];
  const trendFirstCol = {
    hourly: { key: 'hour', label: 'Hour', fmt: hourLabel },
    daily: { key: 'date', label: 'Date' },
    monthly: { key: 'year_month', label: 'Month' },
  };
  const trendTables = {};
  let congestionTable = null;

  function renderTrendTable() {
    const title = { hourly: 'Hourly data', daily: 'Daily data', monthly: 'Monthly data' }[state.trend];
    $('#trendTableTitle').textContent = title;
    const root = $('#trendTable');
    if (state.errors[state.trend]) {
      root.replaceChildren(el('div', { class: 'empty' }, `Could not load data: ${state.errors[state.trend]} `, el('button', { class: 'btn', type: 'button', text: 'Retry', onclick: loadAll })));
      trendTables.current = null;
      return;
    }
    trendTables.current = createClientTable(root, TREND_COLUMNS(trendFirstCol[state.trend]), { pageSize: state.trend === 'hourly' ? 24 : 12 });
    trendTables.current.setRows(state.data[state.trend]);
  }

  function renderCongestionTable() {
    const root = $('#congestionTable');
    if (state.errors.congestion) {
      root.replaceChildren(el('div', { class: 'empty', text: `Could not load data: ${state.errors.congestion}` }));
      return;
    }
    congestionTable = createClientTable(root, [
      { key: 'congestion_level', label: 'Estimated level', render: levelTag },
      { key: 'record_count', label: 'Hours', num: true, fmt: (v) => fmt(v) },
      { key: 'percentage', label: 'Share', num: true, fmt: (v) => `${fmt(v, 2)}%` },
    ], { pageSize: 10, defaultSort: { key: 'record_count', dir: 'desc' } });
    congestionTable.setRows(state.data.congestion);
  }

  // ---------- records (server-side search / sort / paging) ----------
  const RECORD_COLUMNS = [
    { key: 'date_time', label: 'Date & time' },
    { key: 'weather_main', label: 'Weather' },
    { key: 'holiday', label: 'Holiday' },
    { key: 'temp_c', label: 'Temp °C', num: true, fmt: (v) => fmt(v, 1) },
    { key: 'rain_1h', label: 'Rain mm/h', num: true, fmt: (v) => fmt(v, 2) },
    { key: 'traffic_volume', label: 'Volume', num: true, fmt: (v) => fmt(v) },
    { key: 'congestion_level', label: 'Estimated level', render: levelTag },
  ];

  const recParams = () => ({ ...state.filters, search: state.records.search, level: state.records.level, sort: state.records.sort, order: state.records.order });

  async function loadRecords() {
    const r = state.records;
    const seq = ++recSeq;
    r.loading = true; r.error = null;
    renderRecords();
    try {
      const res = await api(`/api/records${qs({ ...recParams(), page: r.page, page_size: r.pageSize })}`);
      if (seq !== recSeq) return;
      Object.assign(r, { rows: res.data, total: res.total, pages: res.pages, page: res.page, loading: false });
    } catch (e) {
      if (seq !== recSeq) return;
      Object.assign(r, { rows: [], total: 0, pages: 1, loading: false, error: e.message });
    }
    renderRecords();
  }

  function renderRecords() {
    const r = state.records;
    const root = $('#recordsTable');
    if (r.error) {
      root.replaceChildren(el('div', { class: 'result err' }, el('strong', { text: 'Could not load records. ' }), r.error, ' ', el('button', { class: 'btn', type: 'button', text: 'Retry', onclick: loadRecords })));
      return;
    }
    const table = buildTable(RECORD_COLUMNS, r.rows, { key: r.sort, dir: r.order }, (key) => {
      r.order = r.sort === key && r.order === 'asc' ? 'desc' : 'asc';
      r.sort = key; r.page = 1; loadRecords();
    }, r.loading ? 'Loading...' : 'No records match your filters.');
    if (r.loading) table.style.opacity = '.55';
    const status = r.loading ? 'Loading...' : `${nf.format(r.total)} record${r.total === 1 ? '' : 's'}`;
    root.replaceChildren(
      el('div', { class: 'table-tools' }, el('span', { class: 'muted', 'aria-live': 'polite', text: status })),
      table,
      el('div', { class: 'pager' },
        el('button', { class: 'btn', type: 'button', text: 'Previous', disabled: r.page <= 1 || r.loading, onclick: () => { r.page--; loadRecords(); } }),
        el('span', { class: 'muted', text: `Page ${r.page} of ${r.pages}` }),
        el('button', { class: 'btn', type: 'button', text: 'Next', disabled: r.page >= r.pages || r.loading, onclick: () => { r.page++; loadRecords(); } })));
  }

  // ---------- CSV export (through fetch so errors are shown, not downloaded) ----------
  async function exportCsv(name, params, filename, button) {
    const original = button ? button.textContent : '';
    if (button) { button.disabled = true; button.textContent = 'Exporting...'; }
    try {
      const res = await fetch(`/api/export/${name}${qs(params)}`);
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error((body && body.error) || `Export failed (${res.status})`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = el('a', { href: url, download: filename });
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
      toast(`Saved ${filename}`);
    } catch (e) {
      toast(e.message || 'Export failed', 'error');
    } finally {
      if (button) { button.disabled = false; button.textContent = original; }
    }
  }

  // ---------- dataset info, badge, banner ----------
  function badgeInfo(ds) {
    if (!ds) return ['Loading...', 'badge-demo'];
    if (ds.kind === 'demo') return ['Demo data (synthetic)', 'badge-demo'];
    if (ds.is_synthetic) return ['Imported ZIP (synthetic sample)', 'badge-demo'];
    return ['Spark results (imported)', 'badge-imported'];
  }

  function renderDataset() {
    const ds = state.dataset;
    const [text, cls] = badgeInfo(ds);
    const badge = $('#sidebarBadge');
    badge.textContent = text; badge.className = `badge ${cls}`;

    const banner = $('#banner');
    if (!ds) { banner.hidden = true; }
    else if (ds.is_synthetic) {
      banner.hidden = false; banner.className = 'banner';
      banner.textContent = 'You are viewing SYNTHETIC sample data. It is randomly generated and is not real traffic. Import your Spark results to see real analysis.';
    } else { banner.hidden = true; }

    const info = $('#datasetInfo');
    if (!ds) { info.replaceChildren(); return; }
    const spark = ds.spark_summary || {};
    const rows = [
      ['Type', ds.kind === 'demo' ? 'Demo (synthetic)' : 'Imported from ZIP'],
      ['Source', ds.source],
      ['Hourly records', fmt(ds.rows)],
      ['Date range', ds.date_min ? `${ds.date_min} to ${ds.date_max}` : '—'],
    ];
    if (ds.imported_at) rows.push(['Imported at (UTC)', ds.imported_at]);
    if (ds.upload_filename) rows.push(['File name', ds.upload_filename]);
    if (spark.generated_at) rows.push(['Generated by notebook', spark.generated_at]);
    const dl = el('dl', {}, rows.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: String(v) })]));
    const parts = [dl, el('p', { class: 'muted', text: ds.disclaimer })];
    if (ds.warnings && ds.warnings.length) {
      parts.push(el('div', { class: 'result warn' }, el('strong', { text: 'Import warnings' }), el('ul', {}, ds.warnings.map((w) => el('li', { text: w })))));
    }
    info.replaceChildren(...parts);

    // Date pickers: limit to the dataset range.
    for (const id of ['startDate', 'endDate']) {
      const input = $(`#${id}`);
      if (ds.date_min) input.min = ds.date_min; else input.removeAttribute('min');
      if (ds.date_max) input.max = ds.date_max; else input.removeAttribute('max');
    }
  }

  async function loadDataset() {
    try {
      state.dataset = await api('/api/dataset');
    } catch (e) {
      state.dataset = null;
      const banner = $('#banner');
      banner.hidden = false; banner.className = 'banner error';
      banner.textContent = e.message;
    }
    renderDataset();
  }

  // ---------- load + render everything ----------
  async function loadAll() {
    const seq = ++loadSeq;
    const q = qs(state.filters);
    state.data = {}; state.errors = {};
    for (const key of Object.keys(ENDPOINTS)) setBox(key, 'loading');
    setBox('trend', 'loading');
    renderKpis();
    const keys = Object.keys(ENDPOINTS);
    const results = await Promise.allSettled(keys.map((k) => api(ENDPOINTS[k] + q)));
    if (seq !== loadSeq) return; // a newer request replaced this one
    results.forEach((res, i) => {
      if (res.status === 'fulfilled') state.data[keys[i]] = res.value.data;
      else state.errors[keys[i]] = res.reason.message;
    });
    for (const key of keys) {
      if (state.errors[key]) setBox(key, 'error', state.errors[key]);
      else if (key !== 'summary') setBox(key, (state.data[key] || []).length ? 'ok' : 'empty');
    }
    renderKpis();
    renderCurrentPage();
  }

  function renderCurrentPage() {
    themeChartDefaults();
    const p = state.page;
    if (p === 'overview') {
      if (!state.errors.hourly && state.data.hourly) chartHourly('chartOverviewHourly');
      if (!state.errors.congestion && state.data.congestion) chartCongestion('chartOverviewCongestion');
      if (!state.errors.weekday && state.data.weekday) chartWeekday('chartOverviewWeekday');
      if (!state.errors.daily && state.data.daily) chartDaily('chartOverviewDaily');
    } else if (p === 'trends') {
      const rows = state.data[state.trend];
      if (state.errors[state.trend]) setBox('trend', 'error', state.errors[state.trend]);
      else if (rows) { setBox('trend', rows.length ? 'ok' : 'empty'); if (rows.length) chartTrend(); }
      renderTrendTable();
    } else if (p === 'congestion') {
      if (state.data.congestion) chartCongestion('chartCongestion');
      if (state.data.weather) chartWeather('chartWeather');
      renderCongestionTable();
    }
  }

  // ---------- navigation ----------
  function showPage(name) {
    if (!PAGES[name]) name = 'overview';
    state.page = name;
    for (const sec of $$('.page')) sec.hidden = sec.dataset.page !== name;
    for (const a of $$('.nav a')) {
      const on = a.dataset.page === name;
      a.classList.toggle('active', on);
      if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    }
    $('#pageTitle').textContent = PAGES[name];
    document.title = `${PAGES[name]} - Traffic Insights`;
    $('.filters').style.visibility = name === 'import' ? 'hidden' : 'visible';
    closeMenu();
    if (name === 'records') loadRecords();
    else renderCurrentPage();
    window.scrollTo({ top: 0 });
  }

  function openMenu() { $('#sidebar').classList.add('open'); $('#scrim').hidden = false; $('#menuBtn').setAttribute('aria-expanded', 'true'); }
  function closeMenu() { $('#sidebar').classList.remove('open'); $('#scrim').hidden = true; $('#menuBtn').setAttribute('aria-expanded', 'false'); }

  // ---------- filters ----------
  function applyFilters(e) {
    if (e) e.preventDefault();
    const start = $('#startDate').value, end = $('#endDate').value;
    if (start && end && start > end) { toast('The start date must not be after the end date.', 'error'); return; }
    state.filters = { start, end };
    state.records.page = 1;
    loadAll();
    if (state.page === 'records') loadRecords();
  }

  function clearFilters() {
    $('#startDate').value = ''; $('#endDate').value = ''; $('#preset').value = '';
    applyFilters();
  }

  function applyPreset() {
    const v = $('#preset').value;
    const ds = state.dataset;
    if (!v || !ds || !ds.date_max) return;
    if (v === 'all') { $('#startDate').value = ds.date_min || ''; $('#endDate').value = ds.date_max; }
    else {
      const end = new Date(`${ds.date_max}T00:00:00Z`);
      const start = new Date(end.getTime() - (Number(v) - 1) * 86400000);
      const iso = (d) => d.toISOString().slice(0, 10);
      $('#startDate').value = iso(start) < ds.date_min ? ds.date_min : iso(start);
      $('#endDate').value = ds.date_max;
    }
    applyFilters();
  }

  // ---------- import / reset ----------
  let chosenFile = null;

  function chooseFile(file) {
    const result = $('#importResult');
    result.replaceChildren();
    chosenFile = null;
    $('#importBtn').disabled = true;
    if (!file) { $('#zipName').textContent = 'No file selected'; return; }
    $('#zipName').textContent = `${file.name} (${(file.size / 1024 / 1024).toFixed(2)} MB)`;
    if (!file.name.toLowerCase().endsWith('.zip')) { showImportError('Please choose a .zip file.', []); return; }
    if (file.size === 0) { showImportError('That file is empty.', []); return; }
    if (file.size > MAX_UPLOAD_MB * 1024 * 1024) { showImportError(`The file is larger than ${MAX_UPLOAD_MB} MB.`, []); return; }
    chosenFile = file;
    $('#importBtn').disabled = false;
  }

  function showImportError(message, details) {
    $('#importResult').replaceChildren(el('div', { class: 'result err', role: 'alert' }, el('strong', { text: message }),
      details && details.length ? el('ul', {}, details.map((d) => el('li', { text: d }))) : null));
  }

  async function submitImport(e) {
    e.preventDefault();
    if (!chosenFile) return;
    const btn = $('#importBtn');
    btn.disabled = true; btn.textContent = 'Importing...';
    $('#importResult').replaceChildren(el('div', { class: 'result' }, el('div', { class: 'spinner' }), ' Validating and importing...'));
    try {
      const form = new FormData();
      form.append('file', chosenFile);
      const res = await api('/api/import', { method: 'POST', body: form });
      const parts = [el('div', { class: 'result ok', role: 'status' }, el('strong', { text: `Import successful: ${nf.format(res.rows)} hourly records loaded.` }))];
      if (res.warnings && res.warnings.length) parts.push(el('div', { class: 'result warn' }, el('strong', { text: 'Warnings' }), el('ul', {}, res.warnings.map((w) => el('li', { text: w })))));
      $('#importResult').replaceChildren(...parts);
      toast('Spark results imported');
      chosenFile = null; $('#zipInput').value = ''; $('#zipName').textContent = 'No file selected';
      await afterDatasetChange();
    } catch (err) {
      showImportError(err.message, err.details);
      btn.disabled = !chosenFile;
    } finally {
      btn.textContent = 'Import Spark Results';
      btn.disabled = !chosenFile;
    }
  }

  async function resetDemo() {
    if (!confirm('Replace the current data with the synthetic demo data?')) return;
    const btn = $('#resetBtn');
    btn.disabled = true;
    try {
      await api('/api/reset', { method: 'POST' });
      $('#importResult').replaceChildren(el('div', { class: 'result ok', role: 'status' }, el('strong', { text: 'Demo data restored.' })));
      toast('Demo data restored');
      await afterDatasetChange();
    } catch (err) {
      showImportError(err.message, err.details);
    } finally {
      btn.disabled = false;
    }
  }

  async function afterDatasetChange() {
    $('#startDate').value = ''; $('#endDate').value = ''; $('#preset').value = '';
    state.filters = { start: '', end: '' };
    state.records.page = 1; state.records.search = ''; state.records.level = '';
    $('#recordSearch').value = ''; $('#recordLevel').value = '';
    await loadDataset();
    await loadAll();
  }

  // ---------- wiring ----------
  function init() {
    $('#filterForm').addEventListener('submit', applyFilters);
    $('#clearFilters').addEventListener('click', clearFilters);
    $('#preset').addEventListener('change', applyPreset);
    $('#menuBtn').addEventListener('click', () => ($('#sidebar').classList.contains('open') ? closeMenu() : openMenu()));
    $('#scrim').addEventListener('click', closeMenu);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMenu(); });
    window.addEventListener('hashchange', () => showPage(location.hash.slice(1)));
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', renderCurrentPage);

    for (const tab of $$('.tab')) {
      tab.addEventListener('click', () => {
        state.trend = tab.dataset.trend;
        for (const t of $$('.tab')) t.setAttribute('aria-selected', String(t === tab));
        renderCurrentPage();
      });
    }
    $('#exportTrend').addEventListener('click', (e) => exportCsv(state.trend, state.filters, `traffic_${state.trend}.csv`, e.currentTarget));
    $('#exportCongestion').addEventListener('click', (e) => exportCsv('congestion', state.filters, 'traffic_congestion.csv', e.currentTarget));
    $('#exportRecords').addEventListener('click', (e) => exportCsv('records', recParams(), 'traffic_records.csv', e.currentTarget));

    let timer;
    $('#recordSearch').addEventListener('input', (e) => {
      clearTimeout(timer);
      timer = setTimeout(() => { state.records.search = e.target.value; state.records.page = 1; loadRecords(); }, 300);
    });
    $('#recordLevel').addEventListener('change', (e) => { state.records.level = e.target.value; state.records.page = 1; loadRecords(); });
    $('#recordPageSize').addEventListener('change', (e) => { state.records.pageSize = Number(e.target.value); state.records.page = 1; loadRecords(); });

    const zip = $('#zipInput'), dz = $('#dropzone');
    zip.addEventListener('change', () => chooseFile(zip.files[0]));
    ['dragenter', 'dragover'].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add('drag'); }));
    ['dragleave', 'drop'].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove('drag'); }));
    dz.addEventListener('drop', (e) => { if (e.dataTransfer.files.length) chooseFile(e.dataTransfer.files[0]); });
    $('#importForm').addEventListener('submit', submitImport);
    $('#resetBtn').addEventListener('click', resetDemo);

    if (typeof Chart === 'undefined') {
      const banner = $('#banner');
      banner.hidden = false; banner.className = 'banner error';
      banner.textContent = 'Chart.js did not load (frontend/vendor/chart.umd.js missing?). Tables still work, charts will not.';
    }
    renderKpis();
    showPage(location.hash.slice(1));
    loadDataset().then(loadAll);
  }

  document.addEventListener('DOMContentLoaded', init);
})();
