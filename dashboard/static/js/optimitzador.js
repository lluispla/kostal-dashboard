/* optimitzador.js — MJF Historical Analysis charts + tables */

(function () {
    'use strict';

    // =========================================================================
    // Historical Analysis
    // =========================================================================

    fetch('/api/optimitzador')
        .then(function (r) { return r.json(); })
        .then(function (d) {
            document.getElementById('loading').style.display = 'none';
            document.getElementById('results').removeAttribute('hidden');
            // Double-rAF ensures the browser has painted the layout
            // so Chart.js can measure canvas dimensions correctly
            requestAnimationFrame(function () {
                requestAnimationFrame(function () {
                    render(d);
                });
            });
        })
        .catch(function (e) {
            document.getElementById('loading').innerHTML = '<p style="color:var(--red);">Error: ' + e + '</p>';
        });

    function render(d) {
        // KPIs
        document.getElementById('kpi-days').textContent = d.total_days + ' (' + d.weekdays + ' feiners, ' + d.weekends + ' caps setm.)';
        document.getElementById('kpi-opt-wd').textContent = pad(d.optimal_weekday) + ':00';
        document.getElementById('kpi-opt-we').textContent = pad(d.optimal_weekend) + ':00';

        var wd22 = d.stats_weekday['22'] ? d.stats_weekday['22'].mean : 0;
        var wdOpt = d.stats_weekday[String(d.optimal_weekday)] ? d.stats_weekday[String(d.optimal_weekday)].mean : 0;
        var we22 = d.stats_weekend['22'] ? d.stats_weekend['22'].mean : 0;
        var weOpt = d.stats_weekend[String(d.optimal_weekend)] ? d.stats_weekend[String(d.optimal_weekend)].mean : 0;
        document.getElementById('kpi-saving-wd').textContent = (wd22 - wdOpt).toFixed(2);
        document.getElementById('kpi-saving-we').textContent = (we22 - weOpt).toFixed(2);

        // Info
        document.getElementById('info-kw').textContent = d.mjf_kw;
        document.getElementById('info-cal').textContent = d.cal_factor.toFixed(2);
        document.getElementById('info-cal-kw').textContent = (d.mjf_kw * d.cal_factor).toFixed(1);
        document.getElementById('info-range').textContent = d.date_range[0] + ' \u2192 ' + d.date_range[1];
        document.getElementById('info-days').textContent = d.total_days;

        // Charts
        renderCostChart('chart-weekday', d.stats_weekday, d.optimal_weekday);
        renderCostChart('chart-weekend', d.stats_weekend, d.optimal_weekend);
        renderWinsChart('chart-wins-wd', d.wins_weekday, d.total_days_wd, d.optimal_weekday);
        renderWinsChart('chart-wins-we', d.wins_weekend, d.total_days_we, d.optimal_weekend);

        // Tables
        renderTable('table-weekday', d.stats_weekday, d.wins_weekday, d.total_days_wd, d.optimal_weekday);
        renderTable('table-weekend', d.stats_weekend, d.wins_weekend, d.total_days_we, d.optimal_weekend);
        renderMonthly('table-monthly', d.monthly);
    }

    function pad(h) { return h < 10 ? '0' + h : '' + h; }

    function renderCostChart(canvasId, stats, optimal) {
        var ctx = document.getElementById(canvasId).getContext('2d');
        var labels = [], means = [], p5s = [], p95s = [], colors = [];
        for (var h = 0; h < 24; h++) {
            var s = stats[String(h)];
            labels.push(pad(h) + ':00');
            means.push(s ? s.mean : 0);
            p5s.push(s ? s.p5 : 0);
            p95s.push(s ? s.p95 : 0);
            colors.push(h === optimal ? '#28a745' : (h === 22 ? '#E63946' : '#0C4DA2'));
        }
        new Chart(ctx, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [
                    { label: 'P5 (millor cas)', data: p5s, backgroundColor: 'rgba(40,167,69,0.15)', borderWidth: 0, order: 2 },
                    { label: 'Cost mitja', data: means, backgroundColor: colors, borderWidth: 0, order: 1 },
                    { label: 'P95 (pitjor cas)', data: p95s, type: 'line', borderColor: '#E63946', backgroundColor: 'transparent', pointRadius: 2, borderWidth: 1.5, order: 0 }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: true, position: 'bottom', labels: { font: { size: 11 } } } },
                scales: {
                    y: { title: { display: true, text: 'EUR / treball' }, beginAtZero: true }
                }
            }
        });
    }

    function renderWinsChart(canvasId, wins, total, optimal) {
        var ctx = document.getElementById(canvasId).getContext('2d');
        var labels = [], data = [], colors = [];
        for (var h = 0; h < 24; h++) {
            labels.push(pad(h) + ':00');
            var w = wins[String(h)] || 0;
            data.push(w);
            colors.push(h === optimal ? '#28a745' : (h === 22 ? '#E63946' : '#6c757d'));
        }
        new Chart(ctx, {
            type: 'bar',
            data: { labels: labels, datasets: [{ label: 'Dies guanyats', data: data, backgroundColor: colors }] },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: { y: { title: { display: true, text: 'Dies' }, beginAtZero: true, ticks: { stepSize: 1 } } }
            }
        });
    }

    function renderTable(tableId, stats, wins, total, optimal) {
        var tbody = document.querySelector('#' + tableId + ' tbody');
        tbody.innerHTML = '';
        for (var h = 0; h < 24; h++) {
            var s = stats[String(h)];
            if (!s) continue;
            var w = wins[String(h)] || 0;
            var pct = total > 0 ? (w / total * 100).toFixed(1) : '0.0';
            var cls = h === optimal ? ' style="background:rgba(40,167,69,0.12); font-weight:700;"' : (h === 22 ? ' style="background:rgba(230,57,70,0.08);"' : '');
            var marker = h === optimal ? ' \u2605' : (h === 22 ? ' (actual)' : '');
            tbody.innerHTML += '<tr' + cls + '><td>' + pad(h) + ':00' + marker + '</td>'
                + '<td>' + s.mean.toFixed(2) + '</td>'
                + '<td>' + s.median.toFixed(2) + '</td>'
                + '<td>' + s.std.toFixed(2) + '</td>'
                + '<td>' + s.p5.toFixed(2) + '</td>'
                + '<td>' + s.p95.toFixed(2) + '</td>'
                + '<td>[' + s.ci_low.toFixed(2) + ' - ' + s.ci_high.toFixed(2) + ']</td>'
                + '<td>' + w + ' (' + pct + '%)</td></tr>';
        }
    }

    function renderMonthly(tableId, monthly) {
        var table = document.getElementById(tableId);
        var months = Object.keys(monthly).sort();
        if (!months.length) return;
        var showHours = [0, 6, 7, 8, 12, 16, 20, 22, 23];
        var thead = '<tr><th>Mes</th>';
        showHours.forEach(function (h) { thead += '<th>' + pad(h) + ':00</th>'; });
        thead += '<th>Millor</th></tr>';
        table.querySelector('thead').innerHTML = thead;
        var tbody = '';
        months.forEach(function (mk) {
            var m = monthly[mk];
            tbody += '<tr><td><strong>' + mk + '</strong></td>';
            showHours.forEach(function (h) {
                var val = m.means[String(h)];
                var cls = h === m.best ? ' style="background:rgba(40,167,69,0.15); font-weight:700;"' : '';
                tbody += '<td' + cls + '>' + (val !== null ? val.toFixed(2) : 'n/a') + '</td>';
            });
            tbody += '<td><strong>' + pad(m.best) + ':00</strong></td></tr>';
        });
        table.querySelector('tbody').innerHTML = tbody;
    }
})();
