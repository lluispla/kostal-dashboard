/* historic.js — Long-term historical charts with zoom/pan */

(function () {
    'use strict';

    // -- Shared zoom/pan config -----------------------------------------------
    const zoomOptions = {
        zoom: {
            wheel: { enabled: true, speed: 0.1 },
            pinch: { enabled: true },
            mode: 'x',
        },
        pan: {
            enabled: true,
            mode: 'x',
        },
        limits: { x: { minRange: 3600000 } },
    };

    // -- Chart instances ------------------------------------------------------
    let chartGenCons = null;
    let chartImpExp = null;
    let chartOmie = null;
    let currentRange = '7d';
    let showEma = false;
    let lastData = null;

    // -- Helpers --------------------------------------------------------------
    function fmt(n) {
        if (n === null || n === undefined) return '--';
        if (n >= 1000) return n.toLocaleString('ca-ES', { maximumFractionDigits: 0 });
        return n.toLocaleString('ca-ES', { maximumFractionDigits: 1 });
    }

    function timeUnit(granularity) {
        return granularity === '1h' ? 'hour' : 'day';
    }

    function emaPeriod(granularity) {
        return granularity === '1h' ? 12 : 7;
    }

    function ema(series, period) {
        if (!series || series.length === 0) return [];
        var alpha = 2 / (period + 1);
        var out = [];
        var prev = series[0].y;
        for (var i = 0; i < series.length; i++) {
            prev = alpha * series[i].y + (1 - alpha) * prev;
            out.push({ x: series[i].x, y: Math.round(prev * 1000) / 1000 });
        }
        return out;
    }

    function emaDataset(label, series, color, period) {
        return {
            label: label,
            data: ema(series, period),
            borderColor: color,
            borderWidth: 2.5,
            borderDash: [8, 4],
            pointRadius: 0,
            fill: false,
            tension: 0.4,
        };
    }

    // -- KPI summary ----------------------------------------------------------
    function updateSummary(summary, fixedRate) {
        document.getElementById('kpi-gen').textContent = fmt(summary.total_generation_kwh);
        document.getElementById('kpi-cons').textContent = fmt(summary.total_consumption_kwh);
        document.getElementById('kpi-self').textContent = fmt(summary.self_consumption_pct);
        document.getElementById('kpi-omie').textContent =
            summary.avg_indexed_eur_kwh !== undefined
                ? summary.avg_indexed_eur_kwh.toFixed(4)
                : '--';
        var effectiveCost = '--';
        if (fixedRate && summary.total_consumption_kwh > 0) {
            effectiveCost = ((summary.total_import_kwh * fixedRate) / summary.total_consumption_kwh).toFixed(4);
        }
        document.getElementById('kpi-effective').textContent = effectiveCost;
        document.getElementById('kpi-indexed-avg').textContent =
            summary.avg_indexed_eur_kwh !== undefined
                ? summary.avg_indexed_eur_kwh.toFixed(4)
                : '--';
    }

    // -- Chart 1: Producció i Consum (no EMA) ---------------------------------
    function initChartGenCons(data) {
        const ctx = document.getElementById('chart-gen-cons').getContext('2d');
        if (chartGenCons) chartGenCons.destroy();

        chartGenCons = new Chart(ctx, {
            type: 'line',
            data: {
                datasets: [
                    {
                        label: 'Generació',
                        data: data.generation,
                        borderColor: '#0C4DA2',
                        backgroundColor: 'rgba(12, 77, 162, 0.15)',
                        fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2,
                    },
                    {
                        label: 'Consum',
                        data: data.consumption,
                        borderColor: '#E63946',
                        backgroundColor: 'rgba(230, 57, 70, 0.10)',
                        fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2,
                    },
                ],
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: { type: 'time', time: { unit: timeUnit(data.granularity), tooltipFormat: data.granularity === '1h' ? 'dd/MM HH:mm' : 'dd/MM/yyyy', displayFormats: { hour: 'HH:mm', day: 'dd/MM' } }, grid: { display: false } },
                    y: { position: 'right', ticks: { callback: function (v) { return v + ' kWh'; } }, grid: { color: '#f0f0f0' } },
                },
                plugins: {
                    zoom: zoomOptions,
                    legend: { position: 'top', labels: { usePointStyle: true, boxWidth: 8 } },
                    tooltip: { callbacks: { label: function (ctx) { return ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(1) + ' kWh'; } } },
                },
            },
        });
    }

    // -- Chart 2: Importació i Exportació -------------------------------------
    function initChartImpExp(data) {
        const ctx = document.getElementById('chart-imp-exp').getContext('2d');
        if (chartImpExp) chartImpExp.destroy();
        var ep = emaPeriod(data.granularity);

        var datasets = [
            { label: 'Importació', data: data.import_kwh, backgroundColor: 'rgba(230, 57, 70, 0.7)', borderColor: '#E63946', borderWidth: 1 },
            { label: 'Exportació', data: data.export_kwh, backgroundColor: 'rgba(40, 167, 69, 0.7)', borderColor: '#28a745', borderWidth: 1 },
        ];
        if (showEma) {
            datasets.push(Object.assign(emaDataset('EMA Importació', data.import_kwh, '#E63946', ep), { type: 'line' }));
            datasets.push(Object.assign(emaDataset('EMA Exportació', data.export_kwh, '#28a745', ep), { type: 'line' }));
        }

        chartImpExp = new Chart(ctx, {
            type: 'bar',
            data: { datasets: datasets },
            options: {
                responsive: true, maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: { type: 'time', time: { unit: timeUnit(data.granularity), tooltipFormat: data.granularity === '1h' ? 'dd/MM HH:mm' : 'dd/MM/yyyy', displayFormats: { hour: 'HH:mm', day: 'dd/MM' } }, grid: { display: false }, offset: true },
                    y: { position: 'right', ticks: { callback: function (v) { return v + ' kWh'; } }, grid: { color: '#f0f0f0' } },
                },
                plugins: {
                    zoom: zoomOptions,
                    legend: { position: 'top', labels: { usePointStyle: true, boxWidth: 8 } },
                    tooltip: { callbacks: { label: function (ctx) { return ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(1) + ' kWh'; } } },
                },
            },
        });
    }

    // -- Chart 3: Preu Indexat ------------------------------------------------
    function initChartOmie(data) {
        const ctx = document.getElementById('chart-omie').getContext('2d');
        if (chartOmie) chartOmie.destroy();
        var ep = emaPeriod(data.granularity);

        var datasets = [
            { label: 'Preu indexat', data: data.omie_avg, borderColor: '#f0ad4e', backgroundColor: 'rgba(240, 173, 78, 0.15)', fill: true, tension: 0.3, pointRadius: 0, borderWidth: showEma ? 1.5 : 2 },
        ];
        if (showEma) {
            datasets.push(emaDataset('EMA Preu indexat', data.omie_avg, '#e08a00', ep));
        }
        if (data._fixed_rate && data.omie_avg.length > 0) {
            var first = data.omie_avg[0].x;
            var last = data.omie_avg[data.omie_avg.length - 1].x;
            datasets.push({ label: 'Tarifa fixa', data: [{ x: first, y: data._fixed_rate }, { x: last, y: data._fixed_rate }], borderColor: '#002B5B', borderDash: [6, 4], borderWidth: 2, pointRadius: 0, fill: false });
        }

        chartOmie = new Chart(ctx, {
            type: 'line',
            data: { datasets: datasets },
            options: {
                responsive: true, maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: { type: 'time', time: { unit: timeUnit(data.granularity), tooltipFormat: data.granularity === '1h' ? 'dd/MM HH:mm' : 'dd/MM/yyyy', displayFormats: { hour: 'HH:mm', day: 'dd/MM' } }, grid: { display: false } },
                    y: { position: 'right', ticks: { callback: function (v) { return v.toFixed(4) + ' €/kWh'; } }, grid: { color: '#f0f0f0' } },
                },
                plugins: {
                    zoom: zoomOptions,
                    legend: { position: 'top', labels: { usePointStyle: true, boxWidth: 8 } },
                    tooltip: { callbacks: { label: function (ctx) { return ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(4) + ' €/kWh'; } } },
                },
            },
        });
    }

    // -- Rebuild charts (called on EMA toggle) --------------------------------
    function rebuildCharts() {
        if (!lastData) return;
        initChartImpExp(lastData);
        initChartOmie(lastData);
    }

    // -- Data loading ---------------------------------------------------------
    async function loadData(range) {
        currentRange = range;
        document.querySelectorAll('.range-btn').forEach(function (btn) {
            btn.classList.toggle('active', btn.dataset.range === range);
        });
        var loading = document.getElementById('historic-loading');
        loading.style.display = 'block';
        try {
            var resp = await fetch('/api/historic/' + range);
            var data = await resp.json();
            if (data.error) { loading.textContent = 'Error: ' + data.error; return; }
            loading.style.display = 'none';
            lastData = data;
            updateSummary(data.summary, data._fixed_rate);
            initChartGenCons(data);
            initChartImpExp(data);
            initChartOmie(data);
        } catch (e) {
            loading.textContent = 'Error carregant dades.';
        }
    }

    // -- Reset zoom buttons ---------------------------------------------------
    document.getElementById('reset-zoom-1').addEventListener('click', function () { if (chartGenCons) chartGenCons.resetZoom(); });
    document.getElementById('reset-zoom-2').addEventListener('click', function () { if (chartImpExp) chartImpExp.resetZoom(); });
    document.getElementById('reset-zoom-3').addEventListener('click', function () { if (chartOmie) chartOmie.resetZoom(); });

    // -- Range button clicks --------------------------------------------------
    document.querySelectorAll('.range-btn').forEach(function (btn) {
        btn.addEventListener('click', function () { loadData(this.dataset.range); });
    });

    // -- EMA toggle -----------------------------------------------------------
    var emaBtn = document.getElementById('toggle-ema');
    if (emaBtn) {
        emaBtn.addEventListener('click', function () {
            showEma = !showEma;
            this.classList.toggle('active', showEma);
            rebuildCharts();
        });
    }

    // -- Fullscreen toggle ----------------------------------------------------
    function resizeCharts() {
        if (chartGenCons) chartGenCons.resize();
        if (chartImpExp) chartImpExp.resize();
        if (chartOmie) chartOmie.resize();
    }

    document.querySelectorAll('.fullscreen-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var wrap = document.getElementById(this.dataset.target);
            wrap.classList.toggle('fullscreen');
            setTimeout(resizeCharts, 50);
        });
    });

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
            document.querySelectorAll('.historic-chart-wrap.fullscreen').forEach(function (el) { el.classList.remove('fullscreen'); });
            setTimeout(resizeCharts, 50);
        }
    });

    // -- Initial load ---------------------------------------------------------
    loadData('7d');

})();
