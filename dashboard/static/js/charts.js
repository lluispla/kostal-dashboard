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

let chartGen     = null;
let chartCons    = null;
let chartGrid    = null;
let chartVoltage = null;
let chartYield   = null;
let chartOmie    = null;

// -- EMA helpers ----------------------------------------------------------
let dashShowEma = false;
let lastPowerData = null;
let lastForecastData = null;
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

/* -- 1a. Generation (with solar forecast overlay) ------------------------- */
function _initGen(data) {
    const ctx = document.getElementById('chart-generation');
    if (!ctx) return;
    var hasForecast = lastForecastData && lastForecastData.forecast_today && lastForecastData.forecast_today.length > 0;
    var datasets = [{
        label: 'Generació real',
        data: data,
        borderColor: COLORS.blue,
        backgroundColor: COLORS.blueFill,
        fill: true,
        tension: 0.3,
        pointRadius: 0,
        borderWidth: dashShowEma ? 1 : 2,
    }];
    if (hasForecast) {
        datasets.push({
            label: 'Previsió solar',
            data: lastForecastData.forecast_today,
            borderColor: COLORS.yellow,
            backgroundColor: 'rgba(240,173,78,0.08)',
            fill: true,
            tension: 0.4,
            pointRadius: 0,
            borderWidth: 2,
            borderDash: [6, 3],
        });
    }
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
                legend: { display: hasForecast || dashShowEma, position: 'top', labels: { usePointStyle: true, boxWidth: 8 } },
                tooltip: { callbacks: { label: c => (c.dataset.label || '') + ': ' + c.parsed.y.toLocaleString('ca') + ' W' } },
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

/* -- 1d. Voltage (3-phase lines + 253V threshold) ------------------------- */
function _initVoltage(l1, l2, l3) {
    const ctx = document.getElementById('chart-voltage');
    if (!ctx) return;
    // Use longest series for threshold endpoints
    var allData = [].concat(l1 || [], l2 || [], l3 || []);
    var thresholdLine = allData.length
        ? [{ x: allData[0].x, y: 253 }, { x: allData[allData.length - 1].x, y: 253 }]
        : [];
    var datasets = [
        {
            label: 'L1',
            data: l1 || [],
            borderColor: '#e67e22',
            backgroundColor: 'rgba(230,126,34,0.10)',
            fill: false,
            tension: 0.3,
            pointRadius: 0,
            borderWidth: 2,
        },
        {
            label: 'L2',
            data: l2 || [],
            borderColor: COLORS.green,
            fill: false,
            tension: 0.3,
            pointRadius: 0,
            borderWidth: 2,
        },
        {
            label: 'L3',
            data: l3 || [],
            borderColor: COLORS.blue,
            fill: false,
            tension: 0.3,
            pointRadius: 0,
            borderWidth: 2,
        },
        {
            label: 'Límit 253 V',
            data: thresholdLine,
            borderColor: COLORS.red,
            borderWidth: 1.5,
            borderDash: [6, 4],
            pointRadius: 0,
            fill: false,
        },
    ];
    chartVoltage = new Chart(ctx, {
        type: 'line',
        data: { datasets: datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: true, position: 'top', labels: { usePointStyle: true, boxWidth: 8 } },
                tooltip: { callbacks: { label: c => c.dataset.label + ': ' + c.parsed.y.toFixed(1) + ' V' } },
            },
            scales: {
                x: _powerXAxis(),
                y: {
                    position: 'right',
                    suggestedMin: 220,
                    suggestedMax: 260,
                    ticks: { color: '#6c757d', callback: v => v + ' V' },
                    grid: { color: '#f0f0f0' },
                },
            },
        },
    });
}

/* -- Public interface ----------------------------------------------------- */
function initPowerCurve(data, forecast) {
    lastPowerData = data;
    if (forecast) lastForecastData = forecast;
    _initGen(data.generation);
    _initCons(data.consumption);
    _initGrid(data.grid);
    _initVoltage(data.voltage_l1 || [], data.voltage_l2 || [], data.voltage_l3 || []);
}

function updatePowerCurve(data, forecast) {
    lastPowerData = data;
    if (forecast) lastForecastData = forecast;
    if (chartGen) chartGen.destroy();
    if (chartCons) chartCons.destroy();
    if (chartGrid) chartGrid.destroy();
    if (chartVoltage) chartVoltage.destroy();
    _initGen(data.generation);
    _initCons(data.consumption);
    _initGrid(data.grid);
    _initVoltage(data.voltage_l1 || [], data.voltage_l2 || [], data.voltage_l3 || []);
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
                            const d = new Date(items[0].parsed.x);
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
                            const d = new Date(items[0].parsed.x);
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

/* -- Voltage gauges (half-circle doughnut per phase, per inverter) --------- */
var gaugeCharts = {};
var gaugeHWM = {};

function _gaugeColor(v) {
    if (v > 253) return '#E63946';
    if (v > 250) return '#e67e22';
    if (v > 245) return '#f0ad4e';
    return '#28a745';
}

// Chart.js plugin to draw HWM tick on gauge arc
var gaugeCenterPlugin = {
    id: 'gaugeCenter',
    afterDraw: function (chart) {
        var meta = chart.getDatasetMeta(0);
        if (!meta || !meta.data || !meta.data[0]) return;
        var hwm = chart.config._gaugeHWM;
        if (!hwm || hwm <= 0) return;

        var ctx = chart.ctx;
        var arc = meta.data[0];
        var cx = arc.x;
        var cy = arc.y;
        var minV = 210, maxV = 260, range = maxV - minV;
        var pct = Math.min(Math.max((hwm - minV) / range, 0), 1);
        var angle = -Math.PI + pct * Math.PI;
        var r = arc.outerRadius;
        var ri = arc.innerRadius;
        var mx = cx + Math.cos(angle) * (ri + (r - ri) * 0.5);
        var my = cy + Math.sin(angle) * (ri + (r - ri) * 0.5);
        ctx.save();
        ctx.beginPath();
        ctx.arc(mx, my, 3, 0, 2 * Math.PI);
        ctx.fillStyle = '#E63946';
        ctx.fill();
        ctx.restore();
    }
};

function _initGaugesForInverter(invId, invData) {
    var phases = ['l1', 'l2', 'l3'];
    phases.forEach(function (ph) {
        var key = invId + '-' + ph;
        var canvas = document.getElementById('gauge-' + key);
        if (!canvas) return;
        var v = invData['voltage_' + ph] || 0;
        gaugeHWM[key] = v;
        _createGauge(canvas, key, v);
        _updateGaugeText(key, v);
    });
}

function initGauges(inversors) {
    _initGaugesForInverter('piko15', inversors.piko_15);
    _initGaugesForInverter('ci50', inversors.piko_ci_50);
}

function _createGauge(canvas, key, voltage) {
    if (gaugeCharts[key]) gaugeCharts[key].destroy();
    var minV = 210, maxV = 260, range = maxV - minV;
    var pct = Math.min(Math.max((voltage - minV) / range, 0), 1);
    var filled = pct * 100;
    var empty = 100 - filled;
    var chart = new Chart(canvas, {
        type: 'doughnut',
        data: {
            datasets: [{
                data: [filled, empty],
                backgroundColor: [_gaugeColor(voltage), '#e9ecef'],
                borderWidth: 0,
            }]
        },
        options: {
            circumference: 180,
            rotation: -90,
            cutout: '70%',
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false }, tooltip: { enabled: false } },
        },
        plugins: [gaugeCenterPlugin],
    });
    chart.config._gaugeValue = voltage;
    chart.config._gaugeHWM = gaugeHWM[key];
    gaugeCharts[key] = chart;
}

function _updateGaugesForInverter(invId, invData) {
    var phases = ['l1', 'l2', 'l3'];
    phases.forEach(function (ph) {
        var key = invId + '-' + ph;
        var canvas = document.getElementById('gauge-' + key);
        if (!canvas) return;
        var v = invData['voltage_' + ph] || 0;
        if (!gaugeHWM[key] || v > gaugeHWM[key]) gaugeHWM[key] = v;
        _createGauge(canvas, key, v);
        _updateGaugeText(key, v);
    });
}

function updateGauges(inversors) {
    _updateGaugesForInverter('piko15', inversors.piko_15);
    _updateGaugesForInverter('ci50', inversors.piko_ci_50);
}

function _updateGaugeText(key, voltage) {
    var valEl = document.getElementById('gauge-val-' + key);
    var hwmEl = document.getElementById('gauge-hwm-' + key);
    if (valEl) {
        valEl.textContent = voltage.toFixed(1) + ' V';
        valEl.style.color = _gaugeColor(voltage);
    }
    if (hwmEl) hwmEl.textContent = 'Màx: ' + gaugeHWM[key].toFixed(1) + ' V';
}

/* -- 4. Compensació gauge (half-circle doughnut) ------------------------- */
var chartCompGauge = null;

function _compGaugeColor(ratio) {
    if (ratio > 100) return '#E63946';
    if (ratio > 80) return '#f0ad4e';
    return '#28a745';
}

function initCompensacioGauge(compensacio) {
    var canvas = document.getElementById('gauge-compensacio');
    if (!canvas || !compensacio) return;

    if (chartCompGauge) chartCompGauge.destroy();

    var ratio = compensacio.gauge_pct || 0;
    var filled = Math.min(ratio / 2, 100); // 0-200 mapped to 0-100 for half circle
    var empty = 100 - filled;

    chartCompGauge = new Chart(canvas, {
        type: 'doughnut',
        data: {
            datasets: [{
                data: [filled, empty],
                backgroundColor: [_compGaugeColor(ratio), '#e9ecef'],
                borderWidth: 0,
            }]
        },
        options: {
            circumference: 180,
            rotation: -90,
            cutout: '70%',
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false }, tooltip: { enabled: false } },
        },
    });
}

function updateCompensacioGauge(compensacio) {
    if (!compensacio) return;
    initCompensacioGauge(compensacio);
    // Update recommendation text
    var rec = document.getElementById('compensacio-rec');
    if (rec) rec.textContent = compensacio.recommendation;
}

/* -- 5. Negative hours bar chart (30d) ----------------------------------- */
var chartNegHours = null;

function initNegHoursChart(negatius) {
    var canvas = document.getElementById('chart-neg-hours');
    if (!canvas || !negatius) return;

    if (chartNegHours) chartNegHours.destroy();

    var data = negatius.daily_neg_30d || [];
    chartNegHours = new Chart(canvas, {
        type: 'bar',
        data: {
            labels: data.map(function(d) { return d.x; }),
            datasets: [{
                label: 'Hores negatives',
                data: data.map(function(d) { return d.y; }),
                backgroundColor: COLORS.red,
                borderRadius: 2,
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
                            var d = new Date(items[0].parsed.x);
                            return d.toLocaleDateString('ca', { day: 'numeric', month: 'short' });
                        },
                        label: function(ctx) { return ctx.parsed.y + ' hores'; }
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
                    beginAtZero: true,
                    position: 'right',
                    ticks: { color: '#6c757d', stepSize: 1, callback: function(v) { return v + 'h'; } },
                    grid: { color: '#f0f0f0' },
                }
            }
        }
    });
}

function updateNegHoursChart(negatius) {
    if (!negatius || !negatius.daily_neg_30d) return;
    if (!chartNegHours) return initNegHoursChart(negatius);
    var data = negatius.daily_neg_30d;
    chartNegHours.data.labels = data.map(function(d) { return d.x; });
    chartNegHours.data.datasets[0].data = data.map(function(d) { return d.y; });
    chartNegHours.update('none');
}

/* -- 6. Maximetre bar chart (contracted vs actual per period) ------------- */
var chartMaximetre = null;

function initMaximetreChart(maximetre) {
    var canvas = document.getElementById('chart-maximetre');
    if (!canvas || !maximetre || !maximetre.data_available) return;

    if (chartMaximetre) chartMaximetre.destroy();

    var periods = ['P1', 'P2', 'P3', 'P4', 'P5', 'P6'];
    var contractedData = [];
    var actualData = [];
    var recommendedData = [];

    periods.forEach(function(p) {
        var pd = maximetre.periods[p];
        if (pd) {
            contractedData.push(pd.contracted_kw);
            actualData.push(pd.actual_max_kw);
            recommendedData.push(pd.recommended_kw);
        } else {
            contractedData.push(0);
            actualData.push(0);
            recommendedData.push(0);
        }
    });

    chartMaximetre = new Chart(canvas, {
        type: 'bar',
        data: {
            labels: periods,
            datasets: [
                {
                    label: 'Contractada (kW)',
                    data: contractedData,
                    backgroundColor: COLORS.navy,
                    borderRadius: 3,
                },
                {
                    label: 'Maxim real (kW)',
                    data: actualData,
                    backgroundColor: actualData.map(function(v, i) {
                        var pct = contractedData[i] > 0 ? (v / contractedData[i]) * 100 : 0;
                        if (pct > 85) return COLORS.red;
                        if (pct > 50) return COLORS.yellow;
                        return COLORS.green;
                    }),
                    borderRadius: 3,
                },
                {
                    label: 'Recomanada (kW)',
                    data: recommendedData,
                    backgroundColor: 'rgba(12,77,162,0.3)',
                    borderColor: COLORS.blue,
                    borderWidth: 1,
                    borderDash: [4, 2],
                    borderRadius: 3,
                },
            ]
        },
        options: {
            indexAxis: 'y',
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { position: 'top', labels: { usePointStyle: true, boxWidth: 8 } },
                tooltip: {
                    callbacks: {
                        label: function(ctx) { return ctx.dataset.label + ': ' + ctx.parsed.x.toFixed(1) + ' kW'; }
                    }
                }
            },
            scales: {
                x: {
                    beginAtZero: true,
                    ticks: { color: '#6c757d', callback: function(v) { return v + ' kW'; } },
                    grid: { color: '#f0f0f0' },
                },
                y: {
                    grid: { display: false },
                    ticks: { color: '#6c757d', font: { weight: 'bold' } },
                }
            }
        }
    });
}

function updateMaximetreChart(maximetre) {
    if (!maximetre || !maximetre.data_available) return;
    initMaximetreChart(maximetre);
}

/* -- 7. Battery SOC curve chart ------------------------------------------ */
var chartBattery = null;

function initBatteryChart(bateria) {
    var canvas = document.getElementById('chart-battery-soc');
    if (!canvas || !bateria || !bateria.soc_curve || bateria.soc_curve.length === 0) return;

    if (chartBattery) chartBattery.destroy();

    chartBattery = new Chart(canvas, {
        type: 'line',
        data: {
            datasets: [{
                label: 'SOC (%)',
                data: bateria.soc_curve,
                borderColor: '#0C4DA2',
                backgroundColor: 'rgba(12,77,162,0.12)',
                fill: true,
                tension: 0.3,
                pointRadius: 0,
                borderWidth: 2,
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
                            var d = new Date(items[0].parsed.x);
                            return d.toLocaleTimeString('ca', { hour: '2-digit', minute: '2-digit' });
                        },
                        label: function(ctx) { return 'SOC: ' + ctx.parsed.y.toFixed(1) + '%'; }
                    }
                }
            },
            scales: {
                x: {
                    type: 'time',
                    time: { unit: 'hour', displayFormats: { hour: 'HH:mm' } },
                    grid: { display: false },
                    ticks: { color: '#6c757d' },
                },
                y: {
                    position: 'right',
                    min: 0,
                    max: 100,
                    ticks: { color: '#6c757d', callback: function(v) { return v + '%'; } },
                    grid: { color: '#f0f0f0' },
                }
            }
        }
    });
}

function updateBatteryChart(bateria) {
    if (!bateria) return;
    initBatteryChart(bateria);
}

/* -- Fullscreen toggle (called from dashboard page) ---------------------- */
function toggleDashFullscreen(wrapId) {
    var wrap = document.getElementById(wrapId);
    if (!wrap) return;
    wrap.classList.toggle('dash-fullscreen');
    // Resize all charts after layout change
    setTimeout(function () {
        [chartGen, chartCons, chartGrid, chartVoltage, chartYield, chartOmie].forEach(function (c) {
            if (c) c.resize();
        });
    }, 50);
}
