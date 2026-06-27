// Consum model supervision page.

const DOW_CA = { mon: 'Dl', tue: 'Dm', wed: 'Dc', thu: 'Dj', fri: 'Dv', sat: 'Ds', sun: 'Dg' };

function hmColor(v, maxV) {
    if (v == null) return 'transparent';
    if (maxV <= 0) return '#fff';
    const t = Math.min(1, v / maxV);
    // green → yellow → red
    const r = Math.round(40 + t * (230 - 40));
    const g = Math.round(167 + t * (57 - 167));
    const b = Math.round(69 + t * (70 - 69));
    return `rgb(${r}, ${g}, ${b})`;
}

async function loadStats() {
    const r = await fetch('/api/consumption-model/stats');
    const d = await r.json();
    document.getElementById('kpi-days').textContent = d.unique_days || 0;
    document.getElementById('kpi-rows').textContent = d.total_rows || 0;
    document.getElementById('kpi-range').textContent = (d.first_day && d.last_day)
        ? `${d.first_day} → ${d.last_day}` : '--';
}

async function loadHeatmap() {
    const loading = document.getElementById('heatmap-loading');
    const table = document.getElementById('heatmap-table');
    try {
        const r = await fetch('/api/consumption-model/heatmap');
        const d = await r.json();
        // Build header
        const header = document.getElementById('heatmap-header');
        header.innerHTML = '<th>Dia</th>' + Array.from({length:24}, (_,h)=>`<th>${String(h).padStart(2,'0')}</th>`).join('');
        // Compute max for color scale
        let maxV = 0;
        for (const row of d.matrix) {
            for (const c of row.hours) {
                if (c.median != null && c.median > maxV) maxV = c.median;
            }
        }
        // Build body
        const body = document.getElementById('heatmap-body');
        body.innerHTML = '';
        for (const row of d.matrix) {
            const tr = document.createElement('tr');
            let html = `<th>${DOW_CA[row.dow] || row.dow}</th>`;
            for (let h = 0; h < 24; h++) {
                const c = row.hours[h];
                if (c.median == null) {
                    html += `<td class="hm-cell hm-na" title="sense dades"></td>`;
                } else {
                    const bg = hmColor(c.median, maxV);
                    html += `<td class="hm-cell" style="background:${bg};" title="${c.median} kW (${c.samples} mostres)">${c.median.toFixed(1)}</td>`;
                }
            }
            tr.innerHTML = html;
            body.appendChild(tr);
        }
        // Compute business-hours baseline average
        let sum = 0, n = 0;
        for (const row of d.matrix) {
            if (row.dow === 'sat' || row.dow === 'sun') continue;
            for (let h = 9; h <= 18; h++) {
                const c = row.hours[h];
                if (c.median != null) { sum += c.median; n++; }
            }
        }
        document.getElementById('kpi-avg-base').textContent = n > 0 ? (sum/n).toFixed(2) : '--';
        loading.hidden = true;
        table.hidden = false;
    } catch (e) {
        loading.textContent = `Error: ${e.message}`;
    }
}

let chartTimeline = null;
async function loadTimeline() {
    const r = await fetch('/api/consumption-model/timeline?days=30');
    const d = await r.json();
    const days = d.days || [];
    if (!days.length) return;
    const labels = days.map(x => x.date.slice(5));  // MM-DD
    const peak = days.map(x => x.peak_concurrent);
    const printerH = days.map(x => x.printer_hours);
    const baseKwh = days.map(x => x.baseline_kwh);

    const peakMax = Math.max(...peak, 0);
    document.getElementById('kpi-peak').textContent = peakMax;

    if (chartTimeline) chartTimeline.destroy();
    const ctx = document.getElementById('chart-timeline').getContext('2d');
    chartTimeline = new Chart(ctx, {
        data: {
            labels: labels,
            datasets: [
                { type: 'bar', label: 'Pic impressores simult.', data: peak, backgroundColor: 'rgba(240, 173, 78, 0.75)', yAxisID: 'y' },
                { type: 'bar', label: 'h · impressora (dia)', data: printerH, backgroundColor: 'rgba(12, 77, 162, 0.55)', yAxisID: 'y' },
                { type: 'line', label: 'Consum base (kWh)', data: baseKwh, borderColor: '#28a745', backgroundColor: 'transparent', yAxisID: 'y1', tension: 0.25, pointRadius: 2, borderWidth: 2 },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                y: { title: { display: true, text: 'Impressores / hores·impressora' }, beginAtZero: true, position: 'left' },
                y1: { title: { display: true, text: 'kWh base' }, position: 'right', beginAtZero: true, grid: { drawOnChartArea: false } },
                x: { ticks: { maxRotation: 60, minRotation: 45, autoSkip: true, maxTicksLimit: 15 } },
            },
        },
    });
}

let chartDay = null;
async function loadDay(dateStr) {
    const status = document.getElementById('day-status');
    status.textContent = 'Carregant...';
    try {
        const r = await fetch(`/api/consumption-model/day/${dateStr}`);
        if (!r.ok) {
            const err = await r.json().catch(() => ({error: 'error'}));
            status.textContent = `Error: ${err.error}`;
            document.getElementById('day-result').hidden = true;
            return;
        }
        const d = await r.json();
        status.textContent = `${d.date} (${d.day_features.dow}) · festiu: ${d.day_features.is_holiday ? 'sí' : 'no'}`;
        document.getElementById('day-result').hidden = false;
        document.getElementById('day-proxy').textContent = d.baseline_proxy_kw;
        document.getElementById('day-load').textContent = d.totals.load_kwh;
        document.getElementById('day-base').textContent = d.totals.baseline_kwh;
        document.getElementById('day-printer').textContent = d.totals.printer_kwh;

        // Hourly chart: load bars + baseline line + concurrent as stepped line
        const labels = [];
        const load = [];
        const base = [];
        const conc = [];
        for (let h = 0; h < 24; h++) {
            labels.push(String(h).padStart(2, '0'));
            const row = d.hours[h] || d.hours[String(h)];
            if (row) {
                load.push(row.load_kw);
                base.push(row.baseline_kw);
                conc.push(row.concurrent_max);
            } else {
                load.push(null); base.push(null); conc.push(null);
            }
        }
        if (chartDay) chartDay.destroy();
        const ctx = document.getElementById('chart-day').getContext('2d');
        chartDay = new Chart(ctx, {
            data: {
                labels: labels,
                datasets: [
                    { type: 'bar', label: 'C&agrave;rrega total (kW)', data: load, backgroundColor: 'rgba(12, 77, 162, 0.45)', yAxisID: 'y' },
                    { type: 'line', label: 'Base (kW)', data: base, borderColor: '#28a745', backgroundColor: 'transparent', yAxisID: 'y', tension: 0.2, pointRadius: 2, borderWidth: 2 },
                    { type: 'line', label: 'Impressores simult.', data: conc, borderColor: '#f0ad4e', backgroundColor: 'transparent', yAxisID: 'y1', stepped: true, pointRadius: 2, borderWidth: 2 },
                ],
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                scales: {
                    y: { title: { display: true, text: 'kW' }, beginAtZero: true },
                    y1: { title: { display: true, text: 'impressores' }, position: 'right', beginAtZero: true, grid: { drawOnChartArea: false }, ticks: { stepSize: 1 } },
                },
            },
        });

        // Events table
        const tbody = document.querySelector('#day-events tbody');
        tbody.innerHTML = '';
        if (!d.events.length) {
            tbody.innerHTML = '<tr><td colspan="4" style="color:var(--muted); text-align:center;">Cap esdeveniment detectat</td></tr>';
        } else {
            for (const e of d.events) {
                const tr = document.createElement('tr');
                const sign = e.type === 'start' ? '+' : '−';
                const color = e.type === 'start' ? '#28a745' : '#E63946';
                tr.innerHTML = `
                    <td>${e.ts.slice(11, 16)}</td>
                    <td style="color:${color};"><strong>${e.type === 'start' ? 'Inici' : 'Atur'}</strong></td>
                    <td>${sign}${e.n_printers}</td>
                    <td>${e.load_kw}</td>
                `;
                tbody.appendChild(tr);
            }
        }
    } catch (e) {
        status.textContent = `Error: ${e.message}`;
    }
}

// Init
(function init() {
    const today = new Date();
    const yesterday = new Date(today.getTime() - 24*60*60*1000);
    const iso = d => d.toISOString().slice(0, 10);
    document.getElementById('day-date').value = iso(yesterday);
    document.getElementById('day-date').max = iso(today);
    document.getElementById('bf-start').value = iso(new Date(today.getTime() - 7*24*60*60*1000));
    document.getElementById('bf-end').value = iso(yesterday);

    document.getElementById('day-load').addEventListener('click', () => {
        loadDay(document.getElementById('day-date').value);
    });
    document.getElementById('bf-run').addEventListener('click', async () => {
        const s = document.getElementById('bf-status');
        s.textContent = 'Reprocessant...';
        const r = await fetch('/api/consumption-model/backfill', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                start: document.getElementById('bf-start').value,
                end: document.getElementById('bf-end').value,
            }),
        });
        const d = await r.json();
        s.textContent = `OK: ${d.days_processed} dies, ${d.rows_written} files (errors: ${d.errors?.length || 0})`;
        loadStats();
        loadHeatmap();
        loadTimeline();
    });

    loadStats();
    loadHeatmap();
    loadTimeline();
    loadDay(iso(yesterday));
})();
