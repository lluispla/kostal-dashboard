/* estadistiques.js — 4-scenario tariff comparison charts + KPIs */

(function () {
    'use strict';

    let chartBills = null;
    let chartSavings = null;
    let chartFlux = null;
    let chartOmie = null;
    let currentRange = '3m';

    // -- Helpers --------------------------------------------------------------
    function fmt(n) {
        if (n === null || n === undefined || n === '--') return '--';
        if (Math.abs(n) >= 1000) return n.toLocaleString('ca-ES', { maximumFractionDigits: 0 });
        return n.toLocaleString('ca-ES', { maximumFractionDigits: 2 });
    }

    function fmtInt(n) {
        if (n === null || n === undefined) return '--';
        return Math.round(n).toLocaleString('ca-ES');
    }

    // -- KPI cards ------------------------------------------------------------
    function updateKPIs(data) {
        var a = data.annual;
        document.getElementById('kpi-iberdrola').textContent = fmt(a.iberdrola_total);
        document.getElementById('kpi-holaluz').textContent = fmt(a.holaluz_total);
        document.getElementById('kpi-som-periodes').textContent = fmt(a.som_periodes_total);
        document.getElementById('kpi-som-indexada').textContent = fmt(a.som_indexada_after_flux);

        // Best saving (largest of all alternatives vs Iberdrola)
        var saving = a.saving_vs_iberdrola;
        document.getElementById('kpi-saving').textContent = fmt(saving);
        var savingWrap = document.getElementById('kpi-saving-wrap');
        if (saving > 0) {
            savingWrap.className = 'value val-green';
        } else if (saving < 0) {
            savingWrap.className = 'value val-red';
        } else {
            savingWrap.className = 'value val-navy';
        }

        // Breakeven
        var be = data.breakeven;
        if (be.breakeven_avg_mwh !== null) {
            document.getElementById('kpi-breakeven').textContent = fmt(be.breakeven_avg_mwh);
            document.getElementById('insight-current').textContent = fmt(be.current_avg_mwh);
            document.getElementById('insight-headroom').textContent = fmt(be.headroom_pct);
            document.getElementById('breakeven-insight').style.display = 'block';
        } else {
            document.getElementById('kpi-breakeven').textContent = '--';
            document.getElementById('breakeven-insight').style.display = 'none';
        }

        // Data quality
        document.getElementById('data-days').textContent = data.days_data;
        var badge = document.getElementById('data-quality');
        if (data.days_data < 7) {
            badge.classList.add('data-quality-warning');
        } else {
            badge.classList.remove('data-quality-warning');
        }
    }

    // -- Chart 1: Monthly bills — 4 grouped bars + green flux markers ---------
    function initChartBills(data) {
        var ctx = document.getElementById('chart-bills').getContext('2d');
        if (chartBills) chartBills.destroy();

        var months = data.monthly.map(function (m) { return m.month; });
        var iberData = data.monthly.map(function (m) { return m.iberdrola_total; });
        var holaData = data.monthly.map(function (m) { return m.holaluz_total; });
        var sperData = data.monthly.map(function (m) { return m.som_periodes_total; });
        var sidxData = data.monthly.map(function (m) { return m.som_indexada_total; });
        var fluxData = data.monthly.map(function (m) { return m.som_indexada_after_flux; });

        chartBills = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: months,
                datasets: [
                    {
                        label: 'Iberdrola',
                        data: iberData,
                        backgroundColor: 'rgba(0, 43, 91, 0.7)',
                        borderColor: '#002B5B',
                        borderWidth: 1,
                    },
                    {
                        label: 'Holaluz',
                        data: holaData,
                        backgroundColor: 'rgba(233, 30, 99, 0.7)',
                        borderColor: '#E91E63',
                        borderWidth: 1,
                    },
                    {
                        label: 'Som Per\u00edodes',
                        data: sperData,
                        backgroundColor: 'rgba(230, 126, 34, 0.7)',
                        borderColor: '#E67E22',
                        borderWidth: 1,
                    },
                    {
                        label: 'Som Indexada',
                        data: sidxData,
                        backgroundColor: 'rgba(12, 77, 162, 0.7)',
                        borderColor: '#0C4DA2',
                        borderWidth: 1,
                    },
                    {
                        label: 'Amb Flux Solar',
                        data: fluxData,
                        type: 'scatter',
                        pointStyle: 'rectRot',
                        pointRadius: 7,
                        backgroundColor: '#28a745',
                        borderColor: '#28a745',
                        borderWidth: 2,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: { grid: { display: false } },
                    y: {
                        position: 'right',
                        ticks: { callback: function (v) { return v.toFixed(0) + ' \u20ac'; } },
                        grid: { color: '#f0f0f0' },
                    },
                },
                plugins: {
                    legend: { position: 'top', labels: { usePointStyle: true, boxWidth: 8 } },
                    tooltip: {
                        callbacks: {
                            label: function (ctx) {
                                return ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(2) + ' \u20ac';
                            },
                        },
                    },
                },
            },
        });
    }

    // -- Chart 2: Cumulative savings vs Iberdrola — 3 lines -------------------
    function initChartSavings(data) {
        var ctx = document.getElementById('chart-savings').getContext('2d');
        if (chartSavings) chartSavings.destroy();

        var months = data.monthly.map(function (m) { return m.month; });
        var cumHola = [];
        var cumPer = [];
        var cumIdx = [];
        var runHola = 0;
        var runPer = 0;
        var runIdx = 0;
        data.monthly.forEach(function (m) {
            runHola += m.iberdrola_total - m.holaluz_total;
            runPer += m.iberdrola_total - m.som_periodes_total;
            runIdx += m.iberdrola_total - m.som_indexada_after_flux;
            cumHola.push(Math.round(runHola * 100) / 100);
            cumPer.push(Math.round(runPer * 100) / 100);
            cumIdx.push(Math.round(runIdx * 100) / 100);
        });

        chartSavings = new Chart(ctx, {
            type: 'line',
            data: {
                labels: months,
                datasets: [
                    {
                        label: 'Holaluz vs Iberdrola',
                        data: cumHola,
                        borderColor: '#E91E63',
                        backgroundColor: 'rgba(233, 30, 99, 0.1)',
                        borderWidth: 2,
                        fill: false,
                        tension: 0.3,
                        pointRadius: 4,
                        pointBackgroundColor: '#E91E63',
                    },
                    {
                        label: 'Som Per\u00edodes vs Iberdrola',
                        data: cumPer,
                        borderColor: '#E67E22',
                        backgroundColor: 'rgba(230, 126, 34, 0.1)',
                        borderWidth: 2,
                        fill: false,
                        tension: 0.3,
                        pointRadius: 4,
                        pointBackgroundColor: '#E67E22',
                    },
                    {
                        label: 'Som Indexada + Flux vs Iberdrola',
                        data: cumIdx,
                        borderColor: '#0C4DA2',
                        backgroundColor: 'rgba(12, 77, 162, 0.1)',
                        borderWidth: 2,
                        fill: true,
                        tension: 0.3,
                        pointRadius: 4,
                        pointBackgroundColor: '#0C4DA2',
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: { grid: { display: false } },
                    y: {
                        position: 'right',
                        ticks: { callback: function (v) { return v.toFixed(0) + ' \u20ac'; } },
                        grid: { color: '#f0f0f0' },
                    },
                },
                plugins: {
                    legend: { position: 'top', labels: { usePointStyle: true, boxWidth: 8 } },
                    tooltip: {
                        callbacks: {
                            label: function (ctx) {
                                var v = ctx.parsed.y;
                                var sign = v >= 0 ? '+' : '';
                                return ctx.dataset.label + ': ' + sign + v.toFixed(2) + ' \u20ac';
                            },
                        },
                    },
                },
            },
        });
    }

    // -- Chart 3: Flux Solar balance ------------------------------------------
    function initChartFlux(data) {
        var ctx = document.getElementById('chart-flux').getContext('2d');
        if (chartFlux) chartFlux.destroy();

        var months = data.monthly.map(function (m) { return m.month; });
        var balances = data.monthly.map(function (m) { return m.flux_balance; });

        chartFlux = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: months,
                datasets: [
                    {
                        label: 'Balan\u00e7 Flux Solar',
                        data: balances,
                        backgroundColor: 'rgba(40, 167, 69, 0.6)',
                        borderColor: '#28a745',
                        borderWidth: 1,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: { grid: { display: false } },
                    y: {
                        position: 'right',
                        ticks: { callback: function (v) { return v.toFixed(0) + ' \u20ac'; } },
                        grid: { color: '#f0f0f0' },
                    },
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: function (ctx) {
                                return 'Balan\u00e7: ' + ctx.parsed.y.toFixed(2) + ' \u20ac';
                            },
                        },
                    },
                },
            },
        });
    }

    // -- Chart 4: OMIE monthly average ----------------------------------------
    function initChartOmie(data) {
        var ctx = document.getElementById('chart-omie-monthly').getContext('2d');
        if (chartOmie) chartOmie.destroy();

        var chartData = data.charts.omie_monthly;
        var labels = chartData.map(function (d) { return d.x; });
        var values = chartData.map(function (d) { return d.y; });

        // Color by value: green < 40, yellow 40-80, red > 80
        var bgColors = values.map(function (v) {
            if (v < 40) return 'rgba(40, 167, 69, 0.7)';
            if (v > 80) return 'rgba(230, 57, 70, 0.7)';
            return 'rgba(240, 173, 78, 0.7)';
        });
        var borderColors = values.map(function (v) {
            if (v < 40) return '#28a745';
            if (v > 80) return '#E63946';
            return '#f0ad4e';
        });

        chartOmie = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [
                    {
                        label: 'OMIE mitja',
                        data: values,
                        backgroundColor: bgColors,
                        borderColor: borderColors,
                        borderWidth: 1,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: { grid: { display: false } },
                    y: {
                        position: 'right',
                        ticks: { callback: function (v) { return v.toFixed(0) + ' \u20ac/MWh'; } },
                        grid: { color: '#f0f0f0' },
                    },
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: function (ctx) {
                                return 'OMIE mitja: ' + ctx.parsed.y.toFixed(2) + ' \u20ac/MWh';
                            },
                        },
                    },
                },
            },
        });
    }

    // -- Monthly table — 12 columns -------------------------------------------
    function populateTable(data) {
        var tbody = document.getElementById('monthly-tbody');
        tbody.innerHTML = '';

        data.monthly.forEach(function (m) {
            var tr = document.createElement('tr');
            tr.innerHTML =
                '<td>' + m.month + '</td>' +
                '<td style="text-align:right">' + m.days + '</td>' +
                '<td style="text-align:right">' + fmtInt(m.import_kwh) + '</td>' +
                '<td style="text-align:right">' + fmtInt(m.export_kwh) + '</td>' +
                '<td style="text-align:right">' + m.avg_omie_mwh.toFixed(1) + '</td>' +
                '<td style="text-align:right">' + fmt(m.iberdrola_total) + '</td>' +
                '<td style="text-align:right">' + fmt(m.holaluz_total) + '</td>' +
                '<td style="text-align:right">' + fmt(m.som_periodes_total) + '</td>' +
                '<td style="text-align:right">' + fmt(m.som_indexada_after_flux) + '</td>' +
                '<td style="text-align:right">' + fmt(m.flux_credit_used) + '</td>' +
                '<td style="text-align:right">' + fmt(m.flux_balance) + '</td>' +
                '<td><span class="winner-badge">' + m.best + '</span></td>';
            tbody.appendChild(tr);
        });

        // Total row
        var a = data.annual;
        var tr = document.createElement('tr');
        tr.className = 'row-total';
        var savingClass = a.saving_vs_iberdrola > 0 ? 'val-green' : (a.saving_vs_iberdrola < 0 ? 'val-red' : '');
        tr.innerHTML =
            '<td>TOTAL</td>' +
            '<td style="text-align:right">' + data.days_data + '</td>' +
            '<td style="text-align:right">' + fmtInt(a.total_import_kwh) + '</td>' +
            '<td style="text-align:right">' + fmtInt(a.total_export_kwh) + '</td>' +
            '<td style="text-align:right">' + a.avg_omie_mwh.toFixed(1) + '</td>' +
            '<td style="text-align:right">' + fmt(a.iberdrola_total) + '</td>' +
            '<td style="text-align:right">' + fmt(a.holaluz_total) + '</td>' +
            '<td style="text-align:right">' + fmt(a.som_periodes_total) + '</td>' +
            '<td style="text-align:right">' + fmt(a.som_indexada_after_flux) + '</td>' +
            '<td colspan="2" style="text-align:right" class="' + savingClass + '">Estalvi: ' + fmt(a.saving_vs_iberdrola) + ' \u20ac</td>' +
            '<td></td>';
        tbody.appendChild(tr);
    }

    // -- Data loading ---------------------------------------------------------
    async function loadData(range) {
        currentRange = range;
        document.querySelectorAll('.range-btn').forEach(function (btn) {
            btn.classList.toggle('active', btn.dataset.range === range);
        });
        var loading = document.getElementById('estadistiques-loading');
        loading.style.display = 'block';
        try {
            var resp = await fetch('/api/estadistiques/' + range);
            var data = await resp.json();
            if (data.error) {
                loading.textContent = 'Error: ' + data.error;
                return;
            }
            loading.style.display = 'none';

            if (!data.monthly || data.monthly.length === 0) {
                loading.style.display = 'block';
                loading.textContent = 'No hi ha prou dades per mostrar.';
                return;
            }

            updateKPIs(data);
            initChartBills(data);
            initChartSavings(data);
            initChartFlux(data);
            initChartOmie(data);
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
    loadData('3m');

})();
