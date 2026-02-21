/* =========================================================================
   Chart.js initialisation — Power Curve, Daily Yield 30d, OMIE Hourly
   With EMA overlay toggle and fullscreen support
   ========================================================================= */

const COLORS = {
    blue:   '#0C4DA2',
    navy:   '#002B5B',
    red:    '#E63946',
    silver: '#C0C0C0',
    green:  '#28a745',
    yellow: '#f0ad4e',
    blueFill: 'rgba(12,77,162,0.15)',
    redFill:  'rgba(230,57,70,0.10)',
};

let chartGen    = null;
let chartCons   = null;
let chartGrid   = null;
let chartYield  = null;
let chartOmie   = null;

// -- EMA helpers ----------------------------------------------------------
let dashShowEma = false;
let lastPowerData = null;
let lastYieldData = null;
let lastMercatData = null;

function _ema(series, period) {
    if (!series || series.length === 0) return [];
    var alpha = 2 / (period + 1);
    var out = [];
    var prev = series[0].y;
    for (var i = 0; i < series.length; i++) {
        prev = alpha * series[i].y + (1 - alpha) * prev;
        out.push({ x: series[i].x, y: Math.round(prev * 100) / 100 });
    }
    return out;
}

function _emaDs(label, series, color, period) {
    return {
        label: label,
        data: _ema(series, period),
        borderColor: color,
        borderWidth: 2.5,
        borderDash: [8, 4],
        pointRadius: 0,
        fill: false,
        tension: 0.4,
    };
}

// -- shared x-axis config for the 3 power charts -------------------------
function _powerXAxis() {
    return {
        type: 'time',
        time: { unit: 'hour', displayFormats: { hour: 'HH:mm' } },
        grid: { display: false },
        ticks: { color: '#6c757d' },
    };
}

// EMA period for 1-minute power data: 30 (half-hour smoothing)
const POWER_EMA_PERIOD = 30;

/* -- 1a. Generation ------------------------------------------------------- */
function _initGen(data) {
    const ctx = document.getElementById('chart-generation');
    if (!ctx) return;
    var datasets = [{
        data: data,
        borderColor: COLORS.blue,
        backgroundColor: COLORS.blueFill,
        fill: true,
        tension: 0.3,
        pointRadius: 0,
        borderWidth: dashShowEma ? 1 : 2,
    }];
    if (dashShowEma) {
        datasets.push(_emaDs('EMA', data, COLORS.blue, POWER_EMA_PERIOD));
    }
    chartGen = new Chart(ctx, {
        type: 'line',
        data: { datasets: datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: dashShowEma, position: 'top', labels: { usePointStyle: true, boxWidth: 8 } },
                tooltip: { callbacks: { label: c => c.parsed.y.toLocaleString('ca') + ' W' } },
            },
            scales: { x: _powerXAxis(), y: {
                position: 'right',
                beginAtZero: true,
                ticks: { color: '#6c757d', callback: v => (v/1000).toFixed(0) + ' kW' },
                grid: { color: '#f0f0f0' },
            }},
        }
    });
}

/* -- 1b. Consumption ------------------------------------------------------ */
function _initCons(data) {
    const ctx = document.getElementById('chart-consumption');
    if (!ctx) return;
    var datasets = [{
        data: data,
        borderColor: COLORS.navy,
        backgroundColor: 'rgba(0,43,91,0.10)',
        fill: true,
        tension: 0.3,
        pointRadius: 0,
        borderWidth: dashShowEma ? 1 : 2,
    }];
    if (dashShowEma) {
        datasets.push(_emaDs('EMA', data, COLORS.navy, POWER_EMA_PERIOD));
    }
    chartCons = new Chart(ctx, {
        type: 'line',
        data: { datasets: datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: dashShowEma, position: 'top', labels: { usePointStyle: true, boxWidth: 8 } },
                tooltip: { callbacks: { label: c => c.parsed.y.toLocaleString('ca') + ' W' } },
            },
            scales: { x: _powerXAxis(), y: {
                position: 'right',
                beginAtZero: true,
                ticks: { color: '#6c757d', callback: v => (v/1000).toFixed(0) + ' kW' },
                grid: { color: '#f0f0f0' },
            }},
        }
    });
}

/* -- 1c. Grid flow (dual-color: red importing, blue exporting) ------------ */
function _initGrid(data) {
    const ctx = document.getElementById('chart-grid');
    if (!ctx) return;
    var datasets = [{
        data: data,
        borderColor: COLORS.silver,
        borderWidth: dashShowEma ? 1 : 1.5,
        fill: {
            target: 'origin',
            above: 'rgba(230,57,70,0.18)',
            below: 'rgba(12,77,162,0.18)',
        },
        tension: 0.3,
        pointRadius: 0,
    }];
    if (dashShowEma) {
        datasets.push(_emaDs('EMA', data, COLORS.navy, POWER_EMA_PERIOD));
    }
    chartGrid = new Chart(ctx, {
        type: 'line',
        data: { datasets: datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: dashShowEma, position: 'top', labels: { usePointStyle: true, boxWidth: 8 } },
                tooltip: { callbacks: {
                    label: function(c) {
                        const v = c.parsed.y;
                        if (v >= 0) return 'Importació: ' + v.toLocaleString('ca') + ' W';
                        return 'Exportació: ' + Math.abs(v).toLocaleString('ca') + ' W';
                    }
                }},
            },
            scales: { x: _powerXAxis(), y: {
                position: 'right',
                ticks: { color: '#6c757d', callback: v => (v/1000).toFixed(0) + ' kW' },
                grid: { color: '#f0f0f0' },
            }},
        }
    });
}

/* -- Public interface ----------------------------------------------------- */
function initPowerCurve(data) {
    lastPowerData = data;
    _initGen(data.generation);
    _initCons(data.consumption);
    _initGrid(data.grid);
}

function updatePowerCurve(data) {
    lastPowerData = data;
    if (chartGen) chartGen.destroy();
    if (chartCons) chartCons.destroy();
    if (chartGrid) chartGrid.destroy();
    _initGen(data.generation);
    _initCons(data.consumption);
    _initGrid(data.grid);
}

/* -- 2. Daily Yield 30d (bar) -------------------------------------------- */
function initYield30d(data) {
    const ctx = document.getElementById('chart-yield');
    if (!ctx) return;
    lastYieldData = data;
    chartYield = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: data.map(d => d.x),
            datasets: [{
                label: 'kWh',
                data: data.map(d => d.y),
                backgroundColor: COLORS.blue,
                borderRadius: 3,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        title: function(items) {
                            const d = new Date(items[0].label);
                            return d.toLocaleDateString('ca', { day: 'numeric', month: 'short' });
                        },
                        label: function(ctx) { return ctx.parsed.y.toFixed(1) + ' kWh'; }
                    }
                }
            },
            scales: {
                x: {
                    type: 'time',
                    time: { unit: 'day', displayFormats: { day: 'dd/MM' } },
                    grid: { display: false },
                    ticks: { color: '#6c757d', maxRotation: 45 },
                },
                y: {
                    position: 'right',
                    ticks: { color: '#6c757d', callback: v => v + ' kWh' },
                    grid: { color: '#f0f0f0' },
                }
            }
        }
    });
}

function updateYield30d(data) {
    lastYieldData = data;
    if (!chartYield) return initYield30d(data);
    chartYield.data.labels = data.map(d => d.x);
    chartYield.data.datasets[0].data = data.map(d => d.y);
    chartYield.update('none');
}

/* -- 3. OMIE Hourly (bar + indexed line + fixed-rate line) --------------- */
function initOmieHourly(mercat) {
    const ctx = document.getElementById('chart-omie');
    if (!ctx) return;
    lastMercatData = mercat;

    const data = mercat.omie_hourly;
    const fixedRate = mercat.fixed_rate;
    const indexedHourly = mercat.indexed_hourly || [];

    const colors = data.map(d => {
        if (d.y < 0.10) return COLORS.blue;
        if (d.y < 0.15) return COLORS.yellow;
        return COLORS.red;
    });

    const fixedLine = data.map(d => ({ x: d.x, y: fixedRate }));

    var datasets = [
        { label: 'OMIE spot (EUR/kWh)', data: data.map(d => d.y), backgroundColor: colors, borderRadius: 2, order: 2 },
        { label: 'Cost real indexat', data: indexedHourly, type: 'line', borderColor: COLORS.red, borderWidth: 2, pointRadius: 0, fill: false, order: 0 },
        { label: 'Tarifa fixa', data: fixedLine, type: 'line', borderColor: COLORS.navy, borderDash: [6, 3], borderWidth: 2, pointRadius: 0, fill: false, order: 1 },
    ];

    chartOmie = new Chart(ctx, {
        type: 'bar',
        data: { labels: data.map(d => d.x), datasets: datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: { position: 'top', labels: { usePointStyle: true, boxWidth: 8 } },
                tooltip: {
                    callbacks: {
                        title: function(items) {
                            const d = new Date(items[0].label);
                            return d.toLocaleTimeString('ca', { hour: '2-digit', minute: '2-digit' });
                        },
                        label: function(ctx) { return ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(4) + ' EUR/kWh'; }
                    }
                }
            },
            scales: {
                x: { type: 'time', time: { unit: 'hour', displayFormats: { hour: 'HH:mm' } }, grid: { display: false }, ticks: { color: '#6c757d' } },
                y: { position: 'right', ticks: { color: '#6c757d', callback: v => v.toFixed(3) }, grid: { color: '#f0f0f0' } }
            }
        }
    });
}

function updateOmieHourly(mercat) {
    if (!chartOmie) return initOmieHourly(mercat);
    lastMercatData = mercat;

    const data = mercat.omie_hourly;
    const fixedRate = mercat.fixed_rate;
    const indexedHourly = mercat.indexed_hourly || [];

    const colors = data.map(d => {
        if (d.y < 0.10) return COLORS.blue;
        if (d.y < 0.15) return COLORS.yellow;
        return COLORS.red;
    });
    const fixedLine = data.map(d => ({ x: d.x, y: fixedRate }));
    chartOmie.data.labels = data.map(d => d.x);
    chartOmie.data.datasets[0].data = data.map(d => d.y);
    chartOmie.data.datasets[0].backgroundColor = colors;
    chartOmie.data.datasets[1].data = indexedHourly;
    chartOmie.data.datasets[2].data = fixedLine;
    chartOmie.update('none');
}

/* -- EMA toggle (called from dashboard page) ----------------------------- */
function toggleDashEma() {
    dashShowEma = !dashShowEma;
    // Rebuild power charts with/without EMA
    if (lastPowerData) updatePowerCurve(lastPowerData);
}

/* -- Fullscreen toggle (called from dashboard page) ---------------------- */
function toggleDashFullscreen(wrapId) {
    var wrap = document.getElementById(wrapId);
    if (!wrap) return;
    wrap.classList.toggle('dash-fullscreen');
    // Resize all charts after layout change
    setTimeout(function () {
        [chartGen, chartCons, chartGrid, chartYield, chartOmie].forEach(function (c) {
            if (c) c.resize();
        });
    }, 50);
}
