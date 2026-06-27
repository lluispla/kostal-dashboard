/* consum_preus.js — Hourly consumption and pricing charts by tariff period */

(function () {
    'use strict';

    var PERIOD_COLORS = {
        P1: '#E63946', P2: '#E67E22', P3: '#f0ad4e',
        P4: '#0C4DA2', P5: '#28a745', P6: '#6c757d'
    };
    var PERIOD_NAMES = {
        P1: 'P1 Punta', P2: 'P2 Pla alt', P3: 'P3 Pla',
        P4: 'P4 Pla baix', P5: 'P5 Vall', P6: 'P6 Supervall'
    };

    var chartConsum = null;
    var chartPreu = null;
    var chartCost = null;
    var currentRange = 'today';

    // -- Helpers --------------------------------------------------------------
    function fmt(n) {
        if (n === null || n === undefined || n === '--') return '--';
        if (Math.abs(n) >= 1000) return n.toLocaleString('ca-ES', { maximumFractionDigits: 0 });
        return n.toLocaleString('ca-ES', { maximumFractionDigits: 2 });
    }

    function fmt5(n) {
        if (n === null || n === undefined) return '--';
        return n.toLocaleString('ca-ES', { minimumFractionDigits: 4, maximumFractionDigits: 5 });
    }

    function periodColor(period) {
        return PERIOD_COLORS[period] || '#6c757d';
    }

    function periodColorAlpha(period, alpha) {
        var hex = PERIOD_COLORS[period] || '#6c757d';
        var r = parseInt(hex.slice(1, 3), 16);
        var g = parseInt(hex.slice(3, 5), 16);
        var b = parseInt(hex.slice(5, 7), 16);
        return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
    }

    // -- KPI cards ------------------------------------------------------------
    function updateKPIs(data) {
        var s = data.summary;
        document.getElementById('kpi-kwh').textContent = fmt(s.total_kwh);
        document.getElementById('kpi-energy-cost').textContent = fmt(s.energy_cost);
        document.getElementById('kpi-power-cost').textContent = fmt(s.power_cost);
        document.getElementById('kpi-total-cost').textContent = fmt(s.subtotal_pre_iva);
        document.getElementById('kpi-avg-rate').textContent = fmt5(s.avg_rate);

        // Days info
        var daysEl = document.getElementById('kpi-total-days');
        if (daysEl && s.days) {
            daysEl.textContent = s.days + (s.days === 1 ? ' dia' : ' dies');
        }

        // Bill breakdown
        var set = function(id, v) { var el = document.getElementById(id); if (el) el.textContent = fmt(v); };
        set('bill-energy', s.energy_cost);
        set('bill-power', s.power_cost);
        set('bill-fixed', s.fixed_cost);
        set('bill-comp', s.compensation ? '-' + fmt(s.compensation) : '0');
        set('bill-iee', s.iee);
        set('bill-iva', s.iva);
        set('bill-total-iva', s.total_cost);

        // Bill forecast (only for "today" range)
        var fcCard = document.getElementById('kpi-forecast-card');
        if (fcCard) {
            if (s.bill_forecast && data.time_range === 'today') {
                var fc = s.bill_forecast;
                document.getElementById('kpi-forecast').textContent = fmt(fc.forecast_pre_iva);
                var detail = fc.days_elapsed + '/' + fc.days_in_month + ' dies reals · '
                    + 'EWMA ' + fmt(fc.ewma_daily) + '€/dia (' + fc.history_days + 'd)';
                document.getElementById('kpi-forecast-detail').textContent = detail;
                fcCard.style.display = '';
            } else {
                fcCard.style.display = 'none';
            }
        }
    }

    // -- Chart 1: Consumption bars colored by period --------------------------
    function initChartConsum(data) {
        var ctx = document.getElementById('chart-consum').getContext('2d');
        if (chartConsum) chartConsum.destroy();

        var labels = data.hourly.map(function (h) { return h.time; });
        var values = data.hourly.map(function (h) { return h.kwh; });
        var bgColors = data.hourly.map(function (h) { return periodColorAlpha(h.period, 0.7); });
        var bdColors = data.hourly.map(function (h) { return periodColor(h.period); });

        chartConsum = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [{
                    label: 'Consum (kWh)',
                    data: values,
                    backgroundColor: bgColors,
                    borderColor: bdColors,
                    borderWidth: 1,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: {
                        grid: { display: false },
                        ticks: {
                            maxTicksLimit: 24,
                            callback: function (val, idx) {
                                var label = this.getLabelForValue(val);
                                if (!label) return '';
                                if (data.aggregation === 'daily') return label.substring(0, 10);
                                var d = new Date(label);
                                return d.getHours() + ':00';
                            },
                        },
                    },
                    y: {
                        position: 'right',
                        ticks: { callback: function (v) { return v.toFixed(1) + ' kWh'; } },
                        grid: { color: '#f0f0f0' },
                    },
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            title: function (items) {
                                var label = items[0].label;
                                if (data.aggregation === 'daily') return label.substring(0, 10);
                                var d = new Date(label);
                                return d.toLocaleDateString('ca-ES') + ' ' + d.getHours() + ':00';
                            },
                            afterTitle: function (items) {
                                var idx = items[0].dataIndex;
                                var h = data.hourly[idx];
                                return h ? (PERIOD_NAMES[h.period] || h.period) : '';
                            },
                            label: function (ctx) {
                                return 'Consum: ' + ctx.parsed.y.toFixed(2) + ' kWh';
                            },
                        },
                    },
                },
            },
        });
    }

    // -- Chart 2: Price bars colored by period --------------------------------
    function initChartPreu(data) {
        var ctx = document.getElementById('chart-preu').getContext('2d');
        if (chartPreu) chartPreu.destroy();

        var labels = data.hourly.map(function (h) { return h.time; });
        var values = data.hourly.map(function (h) { return h.rate; });
        var bgColors = data.hourly.map(function (h) { return periodColorAlpha(h.period, 0.7); });
        var bdColors = data.hourly.map(function (h) { return periodColor(h.period); });

        chartPreu = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [{
                    label: 'Preu (\u20ac/kWh)',
                    data: values,
                    backgroundColor: bgColors,
                    borderColor: bdColors,
                    borderWidth: 1,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: {
                        grid: { display: false },
                        ticks: {
                            maxTicksLimit: 24,
                            callback: function (val, idx) {
                                var label = this.getLabelForValue(val);
                                if (!label) return '';
                                if (data.aggregation === 'daily') return label.substring(0, 10);
                                var d = new Date(label);
                                return d.getHours() + ':00';
                            },
                        },
                    },
                    y: {
                        position: 'right',
                        ticks: { callback: function (v) { return v.toFixed(3) + ' \u20ac'; } },
                        grid: { color: '#f0f0f0' },
                    },
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            title: function (items) {
                                var label = items[0].label;
                                if (data.aggregation === 'daily') return label.substring(0, 10);
                                var d = new Date(label);
                                return d.toLocaleDateString('ca-ES') + ' ' + d.getHours() + ':00';
                            },
                            afterTitle: function (items) {
                                var idx = items[0].dataIndex;
                                var h = data.hourly[idx];
                                return h ? (PERIOD_NAMES[h.period] || h.period) : '';
                            },
                            label: function (ctx) {
                                return 'Preu: ' + ctx.parsed.y.toFixed(5) + ' \u20ac/kWh';
                            },
                        },
                    },
                },
            },
        });
    }

    // -- Chart 3: Stacked cost (energy + power) -------------------------------
    function initChartCost(data) {
        var ctx = document.getElementById('chart-cost').getContext('2d');
        if (chartCost) chartCost.destroy();

        var labels = data.hourly.map(function (h) { return h.time; });
        var energyCosts = data.hourly.map(function (h) { return h.energy_cost; });
        var powerCosts = data.hourly.map(function (h) { return h.power_cost; });
        var bgEnergy = data.hourly.map(function (h) { return periodColorAlpha(h.period, 0.7); });
        var bdEnergy = data.hourly.map(function (h) { return periodColor(h.period); });

        chartCost = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [
                    {
                        label: 'Cost energia',
                        data: energyCosts,
                        backgroundColor: bgEnergy,
                        borderColor: bdEnergy,
                        borderWidth: 1,
                        stack: 'cost',
                    },
                    {
                        label: 'Cost pot\u00e8ncia',
                        data: powerCosts,
                        backgroundColor: 'rgba(180, 180, 180, 0.5)',
                        borderColor: '#999',
                        borderWidth: 1,
                        stack: 'cost',
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: {
                        stacked: true,
                        grid: { display: false },
                        ticks: {
                            maxTicksLimit: 24,
                            callback: function (val, idx) {
                                var label = this.getLabelForValue(val);
                                if (!label) return '';
                                if (data.aggregation === 'daily') return label.substring(0, 10);
                                var d = new Date(label);
                                return d.getHours() + ':00';
                            },
                        },
                    },
                    y: {
                        stacked: true,
                        position: 'right',
                        ticks: { callback: function (v) { return v.toFixed(2) + ' \u20ac'; } },
                        grid: { color: '#f0f0f0' },
                    },
                },
                plugins: {
                    legend: { position: 'top', labels: { usePointStyle: true, boxWidth: 8 } },
                    tooltip: {
                        callbacks: {
                            title: function (items) {
                                var label = items[0].label;
                                if (data.aggregation === 'daily') return label.substring(0, 10);
                                var d = new Date(label);
                                return d.toLocaleDateString('ca-ES') + ' ' + d.getHours() + ':00';
                            },
                            label: function (ctx) {
                                return ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(4) + ' \u20ac';
                            },
                            afterBody: function (items) {
                                var idx = items[0].dataIndex;
                                var h = data.hourly[idx];
                                if (!h) return '';
                                var total = h.energy_cost + h.power_cost;
                                return 'Total: ' + total.toFixed(4) + ' \u20ac';
                            },
                        },
                    },
                },
            },
        });
    }

    // -- Period breakdown table ------------------------------------------------
    function populateTable(data) {
        var tbody = document.getElementById('period-tbody');
        tbody.innerHTML = '';

        data.periods.forEach(function (p) {
            var tr = document.createElement('tr');
            var dot = '<span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:' +
                periodColor(p.period) + ';margin-right:6px;vertical-align:middle;"></span>';
            tr.innerHTML =
                '<td>' + dot + (PERIOD_NAMES[p.period] || p.period) + '</td>' +
                '<td style="text-align:right">' + p.hours + '</td>' +
                '<td style="text-align:right">' + fmt(p.kwh) + '</td>' +
                '<td style="text-align:right">' + fmt(p.energy_cost) + '</td>' +
                '<td style="text-align:right">' + fmt(p.power_cost) + '</td>' +
                '<td style="text-align:right"><strong>' + fmt(p.total_cost) + '</strong></td>' +
                '<td style="text-align:right">' + fmt5(p.avg_rate) + '</td>';
            tbody.appendChild(tr);
        });

        // Total row (sense IVA)
        var s = data.summary;
        var tr = document.createElement('tr');
        tr.className = 'row-total';
        tr.innerHTML =
            '<td><strong>TOTAL (sense IVA)</strong></td>' +
            '<td style="text-align:right">' + (s.days || '--') + 'd</td>' +
            '<td style="text-align:right">' + fmt(s.total_kwh) + '</td>' +
            '<td style="text-align:right">' + fmt(s.energy_cost) + '</td>' +
            '<td style="text-align:right">' + fmt(s.power_cost) + '</td>' +
            '<td style="text-align:right"><strong>' + fmt(s.subtotal_pre_iva) + '</strong></td>' +
            '<td style="text-align:right">' + fmt5(s.avg_rate) + '</td>';
        tbody.appendChild(tr);

        // IVA row
        var trIva = document.createElement('tr');
        trIva.style.color = 'var(--muted)';
        trIva.style.fontSize = '0.85rem';
        trIva.innerHTML =
            '<td colspan="5" style="text-align:right">IVA 21%</td>' +
            '<td style="text-align:right">' + fmt(s.iva) + '</td>' +
            '<td></td>';
        tbody.appendChild(trIva);

        // Total amb IVA row
        var trTotal = document.createElement('tr');
        trTotal.style.color = 'var(--muted)';
        trTotal.style.fontSize = '0.85rem';
        trTotal.innerHTML =
            '<td colspan="5" style="text-align:right">Total amb IVA</td>' +
            '<td style="text-align:right">' + fmt(s.total_cost) + '</td>' +
            '<td></td>';
        tbody.appendChild(trTotal);
    }

    // -- Data loading ---------------------------------------------------------
    var _cachedData = null;

    async function loadData(range) {
        currentRange = range;
        document.querySelectorAll('.range-btn').forEach(function (btn) {
            btn.classList.toggle('active', btn.dataset.range === range);
        });
        var loading = document.getElementById('consum-preus-loading');
        loading.style.display = 'block';
        loading.textContent = 'Carregant dades...';

        try {
            var resp = await fetch('/api/consum-preus/' + range);
            var data = await resp.json();
            if (data.error) {
                loading.textContent = 'Error: ' + data.error;
                return;
            }
            loading.style.display = 'none';
            _cachedData = data;

            if (!data.hourly || data.hourly.length === 0) {
                loading.style.display = 'block';
                loading.textContent = 'No hi ha prou dades per mostrar.';
                return;
            }

            updateKPIs(data);
            initChartConsum(data);
            initChartPreu(data);
            initChartCost(data);
            populateTable(data);
        } catch (e) {
            loading.textContent = 'Error carregant dades.';
        }
    }

    // -- Range button clicks --------------------------------------------------
    document.querySelectorAll('.range-btn').forEach(function (btn) {
        if (btn.dataset.range) {
            btn.addEventListener('click', function () { loadData(this.dataset.range); });
        }
    });

    // -- Initial load ---------------------------------------------------------
    loadData('today');

})();
