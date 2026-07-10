// Programador de Càrregues — fetch /api/programador and render results.

const PERIOD_COLORS = {
    P1: '#E63946', P2: '#E67E22', P3: '#f0ad4e',
    P4: '#0C4DA2', P5: '#28a745', P6: '#6c757d',
};

let chartSchedule = null;
let chartBestDetail = null;
let chartHorizon = null;

// Chart.js plugin: paint period-color background bands behind the horizon chart
const periodBandsPlugin = {
    id: 'periodBands',
    beforeDatasetsDraw(chart, args, opts) {
        const horizon = opts.horizon;
        if (!horizon || !horizon.length) return;
        const { ctx, chartArea, scales } = chart;
        const xScale = scales.x;
        if (!xScale || !chartArea) return;
        ctx.save();
        for (let i = 0; i < horizon.length; i++) {
            const h = horizon[i];
            const color = PERIOD_COLORS[h.period] || '#999';
            const x0 = xScale.getPixelForValue(i);
            const x1 = i + 1 < horizon.length
                ? xScale.getPixelForValue(i + 1)
                : chartArea.right;
            ctx.fillStyle = color + '14';  // ~8% alpha
            ctx.fillRect(x0, chartArea.top, x1 - x0, chartArea.bottom - chartArea.top);
        }
        ctx.restore();
    },
};

// Chart.js plugin: paint the recommended window as a green band + a label box
// spelling out the hours the window ACTUALLY bills. The band edge lines up with
// the price dot of the *next* (excluded) hour, so users read the end-edge price
// as if the window paid it — it doesn't. The label states the true last billed
// hour + its rate + the total, removing that ambiguity.
function _bestWindowSpan(chart, opts) {
    const horizon = opts.horizon, best = opts.best;
    if (!horizon || !best) return null;
    const startIdx = horizon.findIndex(h => h.hour === best.start);
    if (startIdx < 0) return null;
    const endIdx = Math.min(horizon.length - 1, startIdx + (opts.duration || 1) - 1);
    const xScale = chart.scales.x;
    if (!xScale || !chart.chartArea) return null;
    const x0 = xScale.getPixelForValue(startIdx);
    const x1 = endIdx + 1 < horizon.length
        ? xScale.getPixelForValue(endIdx + 1)
        : chart.chartArea.right;
    return { x0, x1 };
}

function _hm(iso) {
    return new Date(iso).toLocaleTimeString('ca-ES', { hour: '2-digit', minute: '2-digit' });
}

const bestWindowPlugin = {
    id: 'bestWindow',
    beforeDatasetsDraw(chart, args, opts) {
        const span = _bestWindowSpan(chart, opts);
        if (!span) return;
        const { ctx, chartArea } = chart;
        ctx.save();
        ctx.fillStyle = 'rgba(40, 167, 69, 0.18)';
        ctx.fillRect(span.x0, chartArea.top, span.x1 - span.x0, chartArea.bottom - chartArea.top);
        ctx.strokeStyle = 'rgba(40, 167, 69, 0.85)';
        ctx.lineWidth = 2;
        ctx.strokeRect(span.x0, chartArea.top, span.x1 - span.x0, chartArea.bottom - chartArea.top);
        ctx.restore();
    },
    // Drawn AFTER the datasets so the orange solar fill doesn't cover the label.
    afterDatasetsDraw(chart, args, opts) {
        const span = _bestWindowSpan(chart, opts);
        if (!span) return;
        const best = opts.best;
        const { ctx, chartArea } = chart;
        const hrs = best.hours || [];
        const last = hrs.length ? hrs[hrs.length - 1] : null;

        const lines = [
            `Finestra recomanada: ${_hm(best.start)}→${_hm(best.end)}`,
            `${opts.duration || hrs.length} h · xarxa ${best.grid_kwh} kWh · solar ${best.solar_kwh} kWh`,
            `Total ${best.total_cost_eur.toFixed(2)} € (IEE + IVA)`,
        ];
        if (last) {
            const lh1 = new Date(new Date(last.hour).getTime() + 3600 * 1000).toISOString();
            lines.push(`darrera hora ${_hm(last.hour)}–${_hm(lh1)} @ ${last.rate_eur_kwh.toFixed(3)} €/kWh`);
        }

        ctx.save();
        const padX = 7, padY = 6, lineH = 15;
        ctx.font = '11px sans-serif';
        let maxw = 0;
        for (let i = 0; i < lines.length; i++) {
            ctx.font = (i === 0 ? 'bold ' : '') + '11px sans-serif';
            maxw = Math.max(maxw, ctx.measureText(lines[i]).width);
        }
        const boxW = maxw + padX * 2;
        const boxH = lines.length * lineH + padY * 2;
        // Anchor at the band's left edge, clamped inside the plot area.
        let bx = span.x0 + 5;
        if (bx + boxW > chartArea.right) bx = chartArea.right - boxW - 5;
        if (bx < chartArea.left) bx = chartArea.left + 5;
        const by = chartArea.top + 5;

        ctx.fillStyle = 'rgba(255, 255, 255, 0.93)';
        ctx.strokeStyle = 'rgba(40, 167, 69, 0.9)';
        ctx.lineWidth = 1;
        ctx.fillRect(bx, by, boxW, boxH);
        ctx.strokeRect(bx, by, boxW, boxH);

        ctx.fillStyle = '#1a7431';
        ctx.textBaseline = 'top';
        for (let i = 0; i < lines.length; i++) {
            ctx.font = (i === 0 ? 'bold ' : '') + '11px sans-serif';
            ctx.fillText(lines[i], bx + padX, by + padY + i * lineH);
        }
        ctx.restore();
    },
};

if (typeof Chart !== 'undefined' && Chart.register) {
    Chart.register(periodBandsPlugin, bestWindowPlugin);
}

function fmtMoney(v) { return (Math.round(v * 100) / 100).toFixed(2); }

function renderKpis(data) {
    const kpis = document.getElementById('sched-kpis');
    if (!data.best || !data.worst) { kpis.hidden = true; return; }
    kpis.hidden = false;
    document.getElementById('kpi-best-label').textContent = data.best.start_label;
    document.getElementById('kpi-best-cost').textContent = fmtMoney(data.best.total_cost_eur);
    document.getElementById('kpi-worst-label').textContent = data.worst.start_label;
    document.getElementById('kpi-worst-cost').textContent = fmtMoney(data.worst.total_cost_eur);
    document.getElementById('kpi-savings').textContent = fmtMoney(data.savings_vs_worst_eur);
    const total = data.best.solar_kwh + data.best.grid_kwh;
    const pct = total > 0 ? Math.round(data.best.solar_kwh / total * 100) : 0;
    document.getElementById('kpi-solar-pct').textContent = pct;
    document.getElementById('kpi-solar-kwh').textContent = data.best.solar_kwh.toFixed(1);
}

function renderScheduleChart(data) {
    const section = document.getElementById('schedule-section');
    section.hidden = false;
    const ctx = document.getElementById('chart-schedule').getContext('2d');
    const labels = data.schedule.map(r => r.start_label);
    const costs = data.schedule.map(r => r.total_cost_eur);
    const bestCost = data.best.total_cost_eur;
    const worstCost = data.worst.total_cost_eur;
    const colors = data.schedule.map(r => {
        const isFcast = r.forecast;
        if (r.total_cost_eur === bestCost) return isFcast ? 'rgba(40,167,69,0.55)' : '#28a745';
        if (r.total_cost_eur === worstCost) return isFcast ? 'rgba(230,57,70,0.55)' : '#E63946';
        return isFcast ? 'rgba(12,77,162,0.45)' : '#0C4DA2';
    });
    if (chartSchedule) chartSchedule.destroy();
    chartSchedule = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [{
                label: 'Cost total (€)',
                data: costs,
                backgroundColor: colors,
                borderWidth: 0,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                y: { title: { display: true, text: '€ (IVA inclòs)' } },
                x: { ticks: { maxRotation: 60, minRotation: 45, autoSkip: true, maxTicksLimit: 24 } },
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (ctx) => {
                            const r = data.schedule[ctx.dataIndex];
                            return [
                                `Cost: ${fmtMoney(r.total_cost_eur)} €`,
                                `Xarxa: ${r.grid_kwh} kWh`,
                                `Solar: ${r.solar_kwh} kWh`,
                                `Període dominant: ${r.dominant_period}`,
                            ];
                        },
                    },
                },
            },
        },
    });
}

function renderBestDetail(data) {
    const section = document.getElementById('best-detail-section');
    const best = data.best;
    if (!best) { section.hidden = true; return; }
    section.hidden = false;

    // Chart: grid kW + solar kW + price line
    const ctx = document.getElementById('chart-best-detail').getContext('2d');
    const labels = best.hours.map(h => {
        const d = new Date(h.hour);
        return d.toLocaleTimeString('ca-ES', { hour: '2-digit', minute: '2-digit' });
    });
    const gridKw = best.hours.map(h => h.grid_kw);
    const solarKw = best.hours.map(h => h.solar_kw);
    const price = best.hours.map(h => h.rate_eur_kwh * 1000); // €/MWh for readability

    if (chartBestDetail) chartBestDetail.destroy();
    chartBestDetail = new Chart(ctx, {
        data: {
            labels: labels,
            datasets: [
                {
                    type: 'bar',
                    label: 'Solar (kW)',
                    data: solarKw,
                    backgroundColor: 'rgba(240, 173, 78, 0.7)',
                    stack: 'load',
                    yAxisID: 'y',
                },
                {
                    type: 'bar',
                    label: 'Xarxa (kW)',
                    data: gridKw,
                    backgroundColor: 'rgba(12, 77, 162, 0.7)',
                    stack: 'load',
                    yAxisID: 'y',
                },
                {
                    type: 'line',
                    label: 'Preu (€/MWh)',
                    data: price,
                    borderColor: '#E63946',
                    backgroundColor: 'transparent',
                    yAxisID: 'y1',
                    tension: 0.2,
                    pointRadius: 3,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                y: { title: { display: true, text: 'kW' }, beginAtZero: true, stacked: true },
                y1: {
                    title: { display: true, text: '€/MWh' },
                    position: 'right',
                    grid: { drawOnChartArea: false },
                },
                x: { stacked: true },
            },
        },
    });

    // Table
    const tbody = document.querySelector('#best-detail-table tbody');
    tbody.innerHTML = '';
    for (const h of best.hours) {
        const d = new Date(h.hour);
        const hrLabel = d.toLocaleString('ca-ES', { weekday: 'short', hour: '2-digit', minute: '2-digit' });
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>${hrLabel}</td>
            <td><span style="display:inline-block; width:12px; height:12px; background:${PERIOD_COLORS[h.period] || '#999'}; border-radius:50%; margin-right:6px;"></span>${h.period}</td>
            <td>${h.rate_eur_kwh.toFixed(4)}</td>
            <td>${h.solar_kw.toFixed(2)}</td>
            <td>${h.grid_kw.toFixed(2)}</td>
            <td>${fmtMoney(h.cost_eur)}</td>
        `;
        tbody.appendChild(tr);
    }
}

function renderHorizon(data) {
    const section = document.getElementById('horizon-section');
    const horizon = data.horizon || [];
    if (!horizon.length) { section.hidden = true; return; }
    section.hidden = false;

    const labels = horizon.map(h => {
        const d = new Date(h.hour);
        return d.toLocaleString('ca-ES', { weekday: 'short', hour: '2-digit', minute: '2-digit' });
    });
    const solar = horizon.map(h => h.solar_kw);
    const baseline = horizon.map(h => h.baseline_kw || 0);
    const hasBaseline = baseline.some(v => v > 0);
    // Two price series so forecast hours render as a separate (dashed) line.
    const priceReal = horizon.map(h => (h.rate_eur_kwh != null && !h.forecast) ? h.rate_eur_kwh * 1000 : null);
    const priceFcast = horizon.map(h => (h.rate_eur_kwh != null && h.forecast) ? h.rate_eur_kwh * 1000 : null);

    if (chartHorizon) chartHorizon.destroy();
    const ctx = document.getElementById('chart-horizon').getContext('2d');
    chartHorizon = new Chart(ctx, {
        data: {
            labels: labels,
            datasets: [
                {
                    type: 'line',
                    label: 'Solar prevista (kW)',
                    data: solar,
                    yAxisID: 'y',
                    backgroundColor: 'rgba(240, 173, 78, 0.55)',
                    borderColor: '#f0ad4e',
                    fill: 'origin',
                    tension: 0.25,
                    pointRadius: 0,
                    borderWidth: 1.5,
                },
                ...(hasBaseline ? [{
                    type: 'line',
                    label: 'Consum base previst (kW)',
                    data: baseline,
                    yAxisID: 'y',
                    borderColor: '#6c757d',
                    backgroundColor: 'rgba(108, 117, 125, 0.25)',
                    fill: 'origin',
                    tension: 0.25,
                    pointRadius: 0,
                    borderWidth: 1.2,
                    borderDash: [2, 3],
                }] : []),
                {
                    type: 'line',
                    label: 'Preu real (€/MWh)',
                    data: priceReal,
                    yAxisID: 'y1',
                    borderColor: '#E63946',
                    backgroundColor: 'transparent',
                    borderWidth: 2,
                    tension: 0.15,
                    pointRadius: 1.5,
                    spanGaps: false,
                },
                {
                    type: 'line',
                    label: 'Preu (previsió €/MWh)',
                    data: priceFcast,
                    yAxisID: 'y1',
                    borderColor: '#E63946',
                    backgroundColor: 'transparent',
                    borderDash: [5, 4],
                    borderWidth: 1.5,
                    tension: 0.15,
                    pointRadius: 1,
                    spanGaps: false,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            scales: {
                y: { title: { display: true, text: 'Solar (kW)' }, beginAtZero: true, position: 'left' },
                y1: {
                    title: { display: true, text: 'Preu (€/MWh)' },
                    position: 'right',
                    beginAtZero: true,
                    grid: { drawOnChartArea: false },
                },
                x: { ticks: { maxRotation: 60, minRotation: 45, autoSkip: true, maxTicksLimit: 24 } },
            },
            plugins: {
                legend: { display: false },
                periodBands: { horizon: horizon },
                bestWindow: { horizon: horizon, best: data.best, duration: data.duration_h },
                tooltip: {
                    callbacks: {
                        afterBody: (items) => {
                            if (!items.length) return '';
                            const i = items[0].dataIndex;
                            const h = horizon[i];
                            const tag = h.forecast ? ' (previsió)' : '';
                            return `Període ${h.period}${tag}`;
                        },
                    },
                },
            },
        },
    });
}

function renderTop5(data) {
    const section = document.getElementById('top5-section');
    if (!data.schedule.length) { section.hidden = true; return; }
    section.hidden = false;

    const sorted = [...data.schedule].sort((a, b) => a.total_cost_eur - b.total_cost_eur).slice(0, 5);
    const tbody = document.querySelector('#top5-table tbody');
    tbody.innerHTML = '';
    sorted.forEach((r, i) => {
        const end = new Date(r.end);
        const endLabel = end.toLocaleString('ca-ES', { weekday: 'short', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
        const tr = document.createElement('tr');
        if (i === 0) tr.style.background = 'rgba(40, 167, 69, 0.1)';
        const fcastBadge = r.forecast ? ' <span title="Inclou hores estimades" style="font-size:0.7rem; padding:1px 6px; background:#ffeeba; color:#856404; border-radius:4px;">previsió</span>' : '';
        tr.innerHTML = `
            <td><strong>#${i + 1}</strong></td>
            <td>${r.start_label}${fcastBadge}</td>
            <td>${endLabel}</td>
            <td><strong>${fmtMoney(r.total_cost_eur)}</strong></td>
            <td>${r.solar_kwh}</td>
            <td>${r.grid_kwh}</td>
            <td><span style="display:inline-block; width:12px; height:12px; background:${PERIOD_COLORS[r.dominant_period] || '#999'}; border-radius:50%; margin-right:6px;"></span>${r.dominant_period}</td>
        `;
        tbody.appendChild(tr);
    });
}

async function runScheduler(ev) {
    if (ev) ev.preventDefault();
    const loadKw = document.getElementById('load_kw').value;
    const duration = document.getElementById('duration_h').value;
    const lookahead = document.getElementById('lookahead_h').value;
    const useForecast = document.getElementById('use_forecast').checked ? 1 : 0;
    const status = document.getElementById('sched-status');
    const banner = document.getElementById('sched-banner');
    banner.hidden = true;
    status.textContent = 'Calculant...';
    try {
        const resp = await fetch(`/api/programador?load_kw=${loadKw}&duration_h=${duration}&lookahead_h=${lookahead}&use_forecast=${useForecast}`);
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ error: 'error desconegut' }));
            status.textContent = `Error: ${err.error || resp.statusText}`;
            return;
        }
        const data = await resp.json();
        if (!data.schedule || !data.schedule.length) {
            status.textContent = data.message || 'No hi ha dades OMIE suficients per a aquest horitzó.';
            document.getElementById('sched-kpis').hidden = true;
            document.getElementById('schedule-section').hidden = true;
            document.getElementById('best-detail-section').hidden = true;
            document.getElementById('top5-section').hidden = true;
            return;
        }
        const realCount = data.real_omie_count || 0;
        const fcastCount = data.forecast_omie_count || 0;
        status.textContent = `${data.schedule.length} inicis avaluats — ${realCount} amb OMIE real, ${fcastCount} amb previsió`;
        if (data.message) {
            banner.hidden = false;
            banner.textContent = data.message;
        }
        renderKpis(data);
        renderHorizon(data);
        renderBestDetail(data);
        renderScheduleChart(data);
        renderTop5(data);
    } catch (e) {
        status.textContent = `Error: ${e.message}`;
    }
}

document.getElementById('sched-form').addEventListener('submit', runScheduler);
// Run once on load
runScheduler();
