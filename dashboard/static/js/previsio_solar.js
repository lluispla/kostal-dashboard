/* previsio_solar.js — Solar forecast page charts */

(function () {
    'use strict';

    let chartForecast3d = null;
    let chartHistVsForecast = null;
    let chartOmie = null;

    function fmt(n) {
        if (n === null || n === undefined) return '--';
        if (n >= 1000) return n.toLocaleString('ca-ES', { maximumFractionDigits: 0 });
        return n.toLocaleString('ca-ES', { maximumFractionDigits: 1 });
    }

    // -- KPIs -----------------------------------------------------------------
    function updateKPIs(data) {
        var fc = data.forecast || {};
        document.getElementById('kpi-forecast-today').textContent = fmt(fc.total_today_kwh);
        document.getElementById('kpi-forecast-tomorrow').textContent = fmt(fc.total_tomorrow_kwh);
        document.getElementById('kpi-forecast-day3').textContent = fmt(fc.total_day3_kwh);
        document.getElementById('kpi-accuracy').textContent = fmt(data.accuracy_pct);
    }

    // -- Chart 1: 3-day forecast + today actual --------------------------------
    function initForecast3d(data) {
        var ctx = document.getElementById('chart-forecast-3d').getContext('2d');
        if (chartForecast3d) chartForecast3d.destroy();

        var fc = data.forecast || {};
        // Combine all 3 days of forecast into one series
        var forecastAll = [].concat(
            fc.forecast_today || [],
            fc.forecast_tomorrow || [],
            fc.forecast_day3 || []
        );
        // Convert watts to kW
        var forecastKW = forecastAll.map(function (p) {
            return { x: p.x, y: Math.round(p.y / 10) / 100 };
        });

        // Today's actual production (from historic_power, only today's date)
        var todayStr = new Date().toISOString().slice(0, 10);
        var actualToday = (data.historic_power || [])
            .filter(function (p) { return p.x && p.x.slice(0, 10) === todayStr; })
            .map(function (p) { return { x: p.x, y: Math.round(p.y / 10) / 100 }; });

        chartForecast3d = new Chart(ctx, {
            type: 'line',
            data: {
                datasets: [
                    {
                        label: 'Previsi\u00f3 (kW)',
                        data: forecastKW,
                        borderColor: '#f0ad4e',
                        backgroundColor: 'rgba(240, 173, 78, 0.10)',
                        borderDash: [6, 3],
                        fill: true,
                        tension: 0.3,
                        pointRadius: 0,
                        borderWidth: 2,
                    },
                    {
                        label: 'Producci\u00f3 real avui (kW)',
                        data: actualToday,
                        borderColor: '#0C4DA2',
                        backgroundColor: 'rgba(12, 77, 162, 0.15)',
                        fill: true,
                        tension: 0.3,
                        pointRadius: 0,
                        borderWidth: 2,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: {
                        type: 'time',
                        time: {
                            unit: 'hour',
                            tooltipFormat: 'dd/MM HH:mm',
                            displayFormats: { hour: 'dd/MM HH:mm' },
                        },
                        grid: { display: false },
                    },
                    y: {
                        position: 'right',
                        ticks: { callback: function (v) { return v + ' kW'; } },
                        grid: { color: '#f0f0f0' },
                    },
                },
                plugins: {
                    legend: { position: 'top', labels: { usePointStyle: true, boxWidth: 8 } },
                    tooltip: {
                        callbacks: {
                            title: function (items) {
                                if (!items.length) return '';
                                var d = new Date(items[0].parsed.x);
                                var dd = String(d.getDate()).padStart(2, '0');
                                var mm = String(d.getMonth() + 1).padStart(2, '0');
                                var hh = String(d.getHours()).padStart(2, '0');
                                var mi = String(d.getMinutes()).padStart(2, '0');
                                return dd + '/' + mm + ' ' + hh + ':' + mi;
                            },
                            label: function (ctx) {
                                return ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(2) + ' kW';
                            },
                        },
                    },
                },
            },
        });
    }

    // -- Chart 2: Historic daily actual vs forecast (bar + line) ---------------
    function initHistVsForecast(data) {
        var ctx = document.getElementById('chart-historic-vs-forecast').getContext('2d');
        if (chartHistVsForecast) chartHistVsForecast.destroy();

        var actual = data.daily_actual || [];
        var forecast = data.daily_forecast || [];

        // Build labels from actual data dates
        var labels = actual.map(function (p) {
            var d = new Date(p.x);
            var dd = String(d.getDate()).padStart(2, '0');
            var mm = String(d.getMonth() + 1).padStart(2, '0');
            return dd + '/' + mm;
        });
        var actualValues = actual.map(function (p) { return p.y; });
        var forecastValues = forecast.map(function (p) { return p.y; });

        chartHistVsForecast = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [
                    {
                        label: 'Producci\u00f3 real (kWh)',
                        data: actualValues,
                        backgroundColor: 'rgba(12, 77, 162, 0.7)',
                        borderColor: '#0C4DA2',
                        borderWidth: 1,
                    },
                    {
                        label: 'Estimaci\u00f3 (kWh)',
                        data: forecastValues,
                        type: 'line',
                        borderColor: '#f0ad4e',
                        backgroundColor: '#f0ad4e',
                        borderWidth: 2,
                        pointRadius: 5,
                        pointStyle: 'triangle',
                        fill: false,
                        tension: 0,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: {
                        type: 'category',
                        grid: { display: false },
                    },
                    y: {
                        position: 'right',
                        ticks: { callback: function (v) { return v + ' kWh'; } },
                        grid: { color: '#f0f0f0' },
                    },
                },
                plugins: {
                    legend: { position: 'top', labels: { usePointStyle: true, boxWidth: 8 } },
                    tooltip: {
                        callbacks: {
                            label: function (ctx) {
                                return ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(1) + ' kWh';
                            },
                        },
                    },
                },
            },
        });
    }

    // -- Chart 3: OMIE spot prices (last 3 days) ------------------------------
    function initOmiePrevisio(data) {
        var ctx = document.getElementById('chart-omie-previsio').getContext('2d');
        if (chartOmie) chartOmie.destroy();

        var omie = data.omie_3d || [];

        chartOmie = new Chart(ctx, {
            type: 'line',
            data: {
                datasets: [
                    {
                        label: 'Preu OMIE spot (\u20ac/kWh)',
                        data: omie,
                        borderColor: '#e67e22',
                        backgroundColor: 'rgba(230, 126, 34, 0.12)',
                        fill: true,
                        tension: 0.3,
                        pointRadius: 0,
                        borderWidth: 2,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: {
                        type: 'time',
                        time: {
                            unit: 'hour',
                            tooltipFormat: 'dd/MM HH:mm',
                            displayFormats: { hour: 'dd/MM HH:mm' },
                        },
                        grid: { display: false },
                    },
                    y: {
                        position: 'right',
                        ticks: { callback: function (v) { return v.toFixed(4) + ' \u20ac'; } },
                        grid: { color: '#f0f0f0' },
                    },
                },
                plugins: {
                    legend: { position: 'top', labels: { usePointStyle: true, boxWidth: 8 } },
                    tooltip: {
                        callbacks: {
                            title: function (items) {
                                if (!items.length) return '';
                                var d = new Date(items[0].parsed.x);
                                var dd = String(d.getDate()).padStart(2, '0');
                                var mm = String(d.getMonth() + 1).padStart(2, '0');
                                var hh = String(d.getHours()).padStart(2, '0');
                                return dd + '/' + mm + ' ' + hh + ':00';
                            },
                            label: function (ctx) {
                                return ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(4) + ' \u20ac/kWh';
                            },
                        },
                    },
                },
            },
        });
    }

    // -- Load data and initialize ---------------------------------------------
    async function loadData() {
        var loading = document.getElementById('previsio-loading');
        loading.style.display = 'block';
        try {
            var resp = await fetch('/api/previsio-solar');
            var data = await resp.json();
            if (data.error) {
                loading.textContent = 'Error: ' + data.error;
                return;
            }
            loading.style.display = 'none';
            updateKPIs(data);
            initForecast3d(data);
            initHistVsForecast(data);
            initOmiePrevisio(data);
            renderAnalysis(data);
        } catch (e) {
            loading.textContent = 'Error carregant dades.';
        }
    }

    function renderAnalysis(data) {
        var el = document.getElementById('previsio-analysis');
        if (!el) return;
        var fc = data.forecast || {};
        var acc = data.accuracy_pct;
        function f(v) { return (v||0).toLocaleString('ca',{maximumFractionDigits:0}); }

        var today = fc.total_today_kwh || 0;
        var tomorrow = fc.total_tomorrow_kwh || 0;
        var day3 = fc.total_day3_kwh || 0;
        var total3d = today + tomorrow + day3;

        var paras = [];

        // Forecast overview
        var trend = tomorrow > today ? 'millorant' : (tomorrow < today * 0.8 ? 'empitjorant' : 'estable');
        paras.push(
            'La previsi\u00f3 solar per als pr\u00f2xims 3 dies \u00e9s de <strong>' + f(today) + '</strong>, ' +
            '<strong>' + f(tomorrow) + '</strong> i <strong>' + f(day3) + ' kWh</strong> respectivament ' +
            '(total: <strong>' + f(total3d) + ' kWh</strong>). La tend\u00e8ncia \u00e9s <strong>' + trend + '</strong>.'
        );

        // Value of forecast
        var valueToday = today * 0.06; // approximate indexed rate
        paras.push(
            'Amb la producci\u00f3 prevista per avui (' + f(today) + ' kWh), la planta cobrir\u00e0 ' +
            'la major part del consum de l\'empresa. ' +
            (today > 200
                ? 'Ser\u00e0 un bon dia solar \u2014 considera programar c\u00e0rregues pesades en hores centrals.'
                : (today > 100
                    ? 'Dia moderat de producci\u00f3. L\'autoconsum cobrir\u00e0 una part significativa.'
                    : 'Dia de baixa producci\u00f3 solar. La major part del consum vindr\u00e0 de la xarxa.'))
        );

        // Accuracy
        if (acc !== null && acc !== undefined) {
            paras.push(
                'La precisi\u00f3 mitjana del model de previsi\u00f3 \u00e9s del <strong>' + acc.toFixed(1) +
                '%</strong>. ' +
                (acc > 85
                    ? 'El model \u00e9s fiable i les prediccions s\u00f3n \u00fatils per a la planificaci\u00f3.'
                    : 'El model est\u00e0 en fase d\'aprenentatge \u2014 la precisi\u00f3 millorar\u00e0 amb m\u00e9s dades hist\u00f2riques.')
            );
        }

        el.innerHTML = paras.map(function(t) {
            return '<p style="font-size:0.92rem;line-height:1.75;margin:0 0 0.6rem;color:var(--text);text-align:justify;">' + t + '</p>';
        }).join('');
    }

    loadData();

})();
