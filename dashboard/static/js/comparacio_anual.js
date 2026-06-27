(function () {
    'use strict';

    var YEAR_COLORS = {
        '2024': { bg: 'rgba(108,117,125,0.15)', border: '#6c757d', label: '2024' },
        '2025': { bg: 'rgba(12,77,162,0.15)', border: '#0C4DA2', label: '2025' },
        '2026': { bg: 'rgba(40,167,69,0.20)', border: '#28a745', label: '2026' },
    };

    var charts = {};
    var currentMode = 'day';

    // --- Init pickers ---
    var dateInput = document.getElementById('yoy-date');
    var weekInput = document.getElementById('yoy-week');
    var monthSelect = document.getElementById('yoy-month');
    var now = new Date();

    dateInput.value = now.toISOString().slice(0, 10);
    dateInput.max = now.toISOString().slice(0, 10);
    dateInput.min = '2024-01-01';

    // Current ISO week number
    var d = new Date(now);
    d.setHours(0, 0, 0, 0);
    d.setDate(d.getDate() + 3 - (d.getDay() + 6) % 7);
    var week1 = new Date(d.getFullYear(), 0, 4);
    var currentWeek = 1 + Math.round(((d - week1) / 86400000 - 3 + (week1.getDay() + 6) % 7) / 7);
    weekInput.value = currentWeek;
    monthSelect.value = now.getMonth() + 1;

    // Enter key on inputs
    dateInput.addEventListener('keydown', function (e) { if (e.key === 'Enter') loadYoY(); });
    weekInput.addEventListener('keydown', function (e) { if (e.key === 'Enter') loadYoY(); });
    monthSelect.addEventListener('change', function () { loadYoY(); });

    // --- Mode switching ---
    window.setMode = function (mode) {
        currentMode = mode;
        document.querySelectorAll('[data-mode]').forEach(function (btn) {
            btn.classList.toggle('active', btn.dataset.mode === mode);
        });
        document.getElementById('picker-day').style.display = mode === 'day' ? '' : 'none';
        document.getElementById('picker-week').style.display = mode === 'week' ? '' : 'none';
        document.getElementById('picker-month').style.display = mode === 'month' ? '' : 'none';
        loadYoY();
    };

    // --- Helpers ---
    function showLoading(show) {
        var el = document.getElementById('yoy-loading');
        if (el) el.style.display = show ? 'flex' : 'none';
    }

    function fmt(v, decimals) {
        if (v === null || v === undefined) return '--';
        return v.toLocaleString('ca', { minimumFractionDigits: decimals || 0, maximumFractionDigits: decimals || 0 });
    }

    var MONTH_NAMES = ['Gener','Febrer','Març','Abril','Maig','Juny','Juliol','Agost','Setembre','Octubre','Novembre','Desembre'];

    function getApiUrl() {
        if (currentMode === 'day') {
            var val = dateInput.value;
            if (!val) return null;
            var parts = val.split('-');
            return { url: '/api/comparacio-anual/' + parseInt(parts[1]) + '/' + parseInt(parts[2]), label: parseInt(parts[2]) + '/' + parseInt(parts[1]) };
        } else if (currentMode === 'week') {
            var w = parseInt(weekInput.value);
            if (!w || w < 1 || w > 53) return null;
            return { url: '/api/comparacio-anual/week/' + w, label: 'Setmana ' + w };
        } else if (currentMode === 'month') {
            var m = parseInt(monthSelect.value);
            return { url: '/api/comparacio-anual/month/' + m, label: MONTH_NAMES[m - 1] };
        } else if (currentMode === 'year') {
            return { url: '/api/comparacio-anual/year/0', label: 'Any complet' };
        }
        return null;
    }

    // --- KPI rendering ---
    function buildKPIs(data) {
        var grid = document.getElementById('yoy-kpi-grid');
        grid.innerHTML = '';
        var years = ['2024', '2025', '2026'];
        var isDaily = (currentMode === 'day');

        years.forEach(function (year) {
            var d = data.years[year];
            var colors = YEAR_COLORS[year];
            var col = document.createElement('div');
            col.style.cssText = 'border: 2px solid ' + colors.border + '; border-radius: 10px; padding: 1rem; background: ' + colors.bg + ';';

            var title = '<div style="font-size: 1.1rem; font-weight: 700; color: ' + colors.border + '; margin-bottom: 0.8rem; text-align: center;">' + year + '</div>';

            if (!d) {
                col.innerHTML = title + '<div style="text-align: center; color: #999; padding: 1rem;">Sense dades</div>';
                grid.appendChild(col);
                return;
            }

            var sourceTag = '';
            if (d.production_source === 'actual') {
                sourceTag = '<span style="background:#28a745;color:#fff;font-size:0.65rem;padding:0.1rem 0.4rem;border-radius:3px;margin-left:0.3rem;">REAL</span>';
            } else if (d.production_source === 'mixed') {
                sourceTag = '<span style="background:#17a2b8;color:#fff;font-size:0.65rem;padding:0.1rem 0.4rem;border-radius:3px;margin-left:0.3rem;">MIXT</span>';
            } else {
                sourceTag = '<span style="background:#f0ad4e;color:#fff;font-size:0.65rem;padding:0.1rem 0.4rem;border-radius:3px;margin-left:0.3rem;">ESTIMAT</span>';
            }

            var rows = [
                { label: 'Irradiància', value: fmt(d.irradiance_kwh_m2, isDaily ? 2 : 1), unit: 'kWh/m²' },
                { label: 'Producció' + sourceTag, value: fmt(d.production_kwh, isDaily ? 1 : 0), unit: 'kWh' },
                { label: 'OMIE mitjà', value: d.omie_avg_eur_mwh != null ? fmt(d.omie_avg_eur_mwh, 1) : '--', unit: '€/MWh' },
                { label: 'Temperatura', value: d.temperature_avg_c != null ? fmt(d.temperature_avg_c, 1) : '--', unit: '°C' },
            ];

            if (d.days) {
                rows.unshift({ label: 'Dies amb dades', value: d.days, unit: 'dies' });
            }

            if (d.estimated_cost_eur > 0) {
                rows.push({ label: 'Cost importació', value: fmt(d.estimated_cost_eur, 2), unit: '€' });
            }

            var html = title;
            rows.forEach(function (r) {
                html += '<div style="display:flex;justify-content:space-between;align-items:baseline;padding:0.3rem 0;border-bottom:1px solid rgba(0,0,0,0.06);">'
                    + '<span style="font-size:0.78rem;color:#666;">' + r.label + '</span>'
                    + '<span style="font-size:1.05rem;font-weight:600;">' + r.value + ' <span style="font-size:0.75rem;color:#888;">' + r.unit + '</span></span>'
                    + '</div>';
            });

            col.innerHTML = html;
            grid.appendChild(col);
        });
    }

    // --- Chart rendering ---
    function extractLabels(data, field) {
        // Collect all x-labels across years for consistent axis
        var labelsSet = {};
        ['2024', '2025', '2026'].forEach(function (year) {
            var d = data.years[year];
            if (!d) return;
            var src = currentMode === 'day' ? (d.hourly || {}) : (d.daily || {});
            var arr = src[field] || [];
            arr.forEach(function (pt) { labelsSet[pt.x] = true; });
        });
        return Object.keys(labelsSet).sort();
    }

    function extractSeries(data, field, labels) {
        var datasets = [];
        ['2024', '2025', '2026'].forEach(function (year) {
            var d = data.years[year];
            if (!d) return;
            var src = currentMode === 'day' ? (d.hourly || {}) : (d.daily || {});
            var arr = src[field] || [];
            if (arr.length === 0) return;

            var colors = YEAR_COLORS[year];
            var dataMap = {};
            arr.forEach(function (pt) { dataMap[pt.x] = pt.y; });
            var points = labels.map(function (l) { return dataMap[l] !== undefined ? dataMap[l] : null; });

            var isDashed = (field === 'production' && d.production_source === 'estimated');
            datasets.push({
                label: year + (isDashed ? ' (est.)' : d.production_source === 'mixed' && field === 'production' ? ' (mixt)' : ''),
                data: points,
                borderColor: colors.border,
                backgroundColor: colors.bg,
                borderWidth: year === '2026' ? 2.5 : 1.8,
                borderDash: isDashed ? [5, 3] : [],
                pointRadius: currentMode === 'day' ? 0 : (labels.length > 60 ? 0 : 2),
                tension: 0.3,
                fill: year === '2026',
            });
        });
        return datasets;
    }

    function createChart(canvasId, data, field, yLabel) {
        var ctx = document.getElementById(canvasId);
        if (!ctx) return;

        if (charts[canvasId]) {
            charts[canvasId].destroy();
        }

        var labels = extractLabels(data, field);
        var datasets = extractSeries(data, field, labels);
        var useBar = (currentMode === 'week') && (field === 'omie' || field === 'production' || field === 'irradiance');

        // Format labels for display
        var displayLabels = labels.map(function (l) {
            if (currentMode === 'day') return l + ':00';
            // MM-DD → D/M
            if (l.indexOf('-') > 0) {
                var parts = l.split('-');
                return parseInt(parts[1]) + '/' + parseInt(parts[0]);
            }
            return l;
        });

        charts[canvasId] = new Chart(ctx, {
            type: useBar ? 'bar' : 'line',
            data: {
                labels: displayLabels,
                datasets: useBar ? datasets.map(function (ds) {
                    return Object.assign({}, ds, {
                        type: 'bar',
                        fill: false,
                        borderWidth: 1,
                        backgroundColor: ds.borderColor + '88',
                        barPercentage: labels.length > 60 ? 1.0 : 0.85,
                        categoryPercentage: labels.length > 60 ? 1.0 : 0.85,
                    });
                }) : datasets,
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: { position: 'top', labels: { usePointStyle: true, padding: 12 } },
                    tooltip: {
                        callbacks: {
                            label: function (ctx) {
                                var v = ctx.parsed.y;
                                return ctx.dataset.label + ': ' + (v !== null ? v.toFixed(1) : '--') + ' ' + yLabel;
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        grid: { display: false },
                        ticks: {
                            maxTicksLimit: currentMode === 'year' ? 12 : (currentMode === 'month' ? 15 : 24),
                            maxRotation: 45,
                        }
                    },
                    y: {
                        beginAtZero: true,
                        title: { display: true, text: yLabel },
                        grid: { color: 'rgba(0,0,0,0.05)' },
                    }
                }
            }
        });
    }

    function updateChartTitles() {
        var unitIrr = currentMode === 'day' ? '(W/m²)' : '(kWh/m²/dia)';
        var unitProd = '(kWh' + (currentMode === 'day' ? '' : '/dia') + ')';
        document.getElementById('chart-title-irr').textContent = 'Irradiància solar ' + unitIrr;
        document.getElementById('chart-title-prod').textContent = 'Producció solar ' + unitProd;
        document.getElementById('chart-title-omie').textContent = 'Preu OMIE spot (€/MWh)';
        document.getElementById('chart-title-temp').textContent = 'Temperatura (°C)';
    }

    function updateCharts(data) {
        updateChartTitles();
        var irrUnit = currentMode === 'day' ? 'W/m²' : 'kWh/m²';
        createChart('chart-yoy-irradiance', data, 'irradiance', irrUnit);
        createChart('chart-yoy-production', data, 'production', 'kWh');
        createChart('chart-yoy-omie', data, 'omie', '€/MWh');
        createChart('chart-yoy-temperature', data, 'temperature', '°C');
    }

    // --- Main load function ---
    window.loadYoY = function () {
        var info = getApiUrl();
        if (!info) return;

        document.getElementById('yoy-date-label').textContent = info.label;
        showLoading(true);

        fetch(info.url)
            .then(function (r) { return r.json(); })
            .then(function (data) {
                buildKPIs(data);
                updateCharts(data);
                showLoading(false);
            })
            .catch(function (err) {
                console.error('YoY fetch error:', err);
                showLoading(false);
            });
    };

    // Auto-load on page open
    loadYoY();
})();
