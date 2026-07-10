/* =========================================================================
   Amortització — Dual perspective: vs Iberdrola Fix + vs Indexada Actual
   ========================================================================= */

(function () {
    var COLORS = {
        green: '#28a745', greenLight: '#5cb85c',
        blue: '#0C4DA2', navy: '#002B5B',
        red: '#E63946',
    };

    fetch('/api/amortitzacio')
        .then(function (r) { return r.json(); })
        .then(function (data) {
            document.getElementById('amort-loading').style.display = 'none';
            document.getElementById('amort-content').style.display = 'block';
            renderKPIs(data);
            renderChart(data);
            renderTable(data);
            renderAnalysis(data);
        })
        .catch(function (err) {
            document.getElementById('amort-loading').textContent =
                'Error carregant dades: ' + err.message;
        });

    function fmt(v) {
        return v.toLocaleString('ca', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    function renderKPIs(data) {
        var inv = data.investment;

        // Shared
        document.getElementById('amort-cost').innerHTML =
            inv.cost.toLocaleString('ca') + ' <span class="unit">\u20ac</span>';
        document.getElementById('amort-date').textContent = 'Des de ' + inv.date;
        document.getElementById('amort-gen').innerHTML =
            data.total_gen_kwh.toLocaleString('ca', { maximumFractionDigits: 0 }) +
            ' <span class="unit">kWh</span>';
        document.getElementById('amort-months').textContent =
            inv.months_elapsed + ' mesos de dades';

        // LCOE — cost real del kWh solar
        var l = data.lcoe;
        if (l) {
            function fmtc(v) {
                return v.toLocaleString('ca', { minimumFractionDigits: 4, maximumFractionDigits: 4 });
            }
            document.getElementById('lcoe-lifetime').innerHTML =
                fmtc(l.lcoe_lifetime) + ' <span class="unit">€/kWh</span>';
            document.getElementById('lcoe-basis').textContent = l.based_on_real
                ? 'producció real (' + Math.round(l.annual_kwh_proj).toLocaleString('ca') + ' kWh/any)'
                : 'estim. disseny (poques dades encara)';
            document.getElementById('lcoe-todate').innerHTML =
                fmtc(l.cost_per_kwh_todate) + ' <span class="unit">€/kWh</span>';
            document.getElementById('lcoe-spec').innerHTML =
                fmtc(l.lcoe_spec) + ' <span class="unit">€/kWh</span>';
            document.getElementById('lcoe-kwp').textContent =
                l.system_kwp + ' kWp × ' + Math.round(l.annual_kwh_spec / l.system_kwp) + ' kWh/kWp';
        }

        // vs Iberdrola
        document.getElementById('amort-savings-iber').innerHTML = fmt(data.total_savings_iber) + ' <span class="unit">\u20ac</span>';
        document.getElementById('amort-pct-iber').innerHTML = data.payback_pct_iber.toFixed(1) + ' <span class="unit">%</span>';
        document.getElementById('amort-monthly-iber').innerHTML = fmt(data.avg_monthly_savings_iber) + ' <span class="unit">\u20ac/mes</span>';
        document.getElementById('amort-payback-iber').textContent = data.projected_payback_date_iber;

        var pctIber = Math.min(data.payback_pct_iber, 100);
        document.getElementById('amort-bar-iber').style.width = pctIber + '%';
        document.getElementById('amort-bar-text-iber').textContent = pctIber.toFixed(1) + '%';

        // vs Indexed
        document.getElementById('amort-savings-idx').innerHTML = fmt(data.total_savings) + ' <span class="unit">\u20ac</span>';
        document.getElementById('amort-pct-idx').innerHTML = data.payback_pct.toFixed(1) + ' <span class="unit">%</span>';
        document.getElementById('amort-monthly-idx').innerHTML = fmt(data.avg_monthly_savings) + ' <span class="unit">\u20ac/mes</span>';
        document.getElementById('amort-payback-idx').textContent = data.projected_payback_date;

        var pctIdx = Math.min(data.payback_pct, 100);
        document.getElementById('amort-bar-idx').style.width = pctIdx + '%';
        document.getElementById('amort-bar-text-idx').textContent = pctIdx.toFixed(1) + '%';

        // vs Real invoices
        document.getElementById('amort-savings-real').innerHTML = fmt(data.total_savings_real) + ' <span class="unit">\u20ac</span>';
        document.getElementById('amort-pct-real').innerHTML = data.payback_pct_real.toFixed(1) + ' <span class="unit">%</span>';
        document.getElementById('amort-monthly-real').innerHTML = fmt(data.avg_monthly_savings_real) + ' <span class="unit">\u20ac/mes</span>';
        document.getElementById('amort-payback-real').textContent = data.projected_payback_date_real;
        document.getElementById('amort-iber-avg').textContent = (data.iber_real_monthly || 2067).toLocaleString('ca');

        var pctReal = Math.min(data.payback_pct_real, 100);
        document.getElementById('amort-bar-real').style.width = pctReal + '%';
        document.getElementById('amort-bar-text-real').textContent = pctReal.toFixed(1) + '%';
    }

    function renderChart(data) {
        var canvas = document.getElementById('chart-amort');
        if (!canvas) return;

        var monthlyIber = data.chart_monthly_iber || [];
        var monthlyIdx = data.chart_monthly_idx || [];
        var monthlyReal = data.chart_monthly_real || [];
        var cumulIber = data.chart_cumulative_iber || [];
        var cumulIdx = data.chart_cumulative_idx || [];
        var cumulReal = data.chart_cumulative_real || [];
        var invCost = data.investment.cost;

        var labels = monthlyReal.map(function (d) { return d.x; });

        // Investment threshold line
        var investLine = labels.length > 0
            ? [{ x: labels[0], y: invCost }, { x: labels[labels.length - 1], y: invCost }]
            : [];

        new Chart(canvas, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [
                    {
                        label: 'Mensual vs Factura Real',
                        data: monthlyReal.map(function (d) { return d.y; }),
                        backgroundColor: COLORS.red + '77',
                        borderRadius: 3,
                        order: 4,
                        yAxisID: 'y',
                    },
                    {
                        label: 'Mensual vs Iberdrola',
                        data: monthlyIber.map(function (d) { return d.y; }),
                        backgroundColor: COLORS.green + '77',
                        borderRadius: 3,
                        order: 5,
                        yAxisID: 'y',
                    },
                    {
                        label: 'Mensual vs Indexada',
                        data: monthlyIdx.map(function (d) { return d.y; }),
                        backgroundColor: COLORS.blue + '77',
                        borderRadius: 3,
                        order: 6,
                        yAxisID: 'y',
                    },
                    {
                        label: 'Acumulat vs Factura Real',
                        data: cumulReal,
                        type: 'line',
                        borderColor: COLORS.red,
                        borderWidth: 3,
                        pointRadius: 4,
                        fill: false,
                        order: 0,
                        yAxisID: 'y1',
                    },
                    {
                        label: 'Acumulat vs Iberdrola',
                        data: cumulIber,
                        type: 'line',
                        borderColor: COLORS.green,
                        borderWidth: 2,
                        pointRadius: 3,
                        fill: false,
                        order: 1,
                        yAxisID: 'y1',
                    },
                    {
                        label: 'Acumulat vs Indexada',
                        data: cumulIdx,
                        type: 'line',
                        borderColor: COLORS.blue,
                        borderWidth: 2,
                        pointRadius: 3,
                        fill: false,
                        order: 2,
                        yAxisID: 'y1',
                    },
                    {
                        label: 'Cost instal\u00b7laci\u00f3',
                        data: investLine,
                        type: 'line',
                        borderColor: '#333',
                        borderWidth: 2,
                        borderDash: [8, 4],
                        pointRadius: 0,
                        fill: false,
                        order: 3,
                        yAxisID: 'y1',
                    },
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: { position: 'top', labels: { usePointStyle: true, boxWidth: 8 } },
                    tooltip: {
                        callbacks: {
                            label: function (ctx) {
                                return ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(2) + ' \u20ac';
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        grid: { display: false },
                        ticks: { color: '#6c757d' },
                    },
                    y: {
                        position: 'left',
                        title: { display: true, text: 'Estalvi mensual (\u20ac)' },
                        ticks: { color: '#6c757d', callback: function (v) { return v.toFixed(0) + ' \u20ac'; } },
                        grid: { color: '#f0f0f0' },
                    },
                    y1: {
                        position: 'right',
                        title: { display: true, text: 'Acumulat (\u20ac)' },
                        ticks: { color: '#6c757d', callback: function (v) { return v.toFixed(0) + ' \u20ac'; } },
                        grid: { display: false },
                    },
                },
            }
        });
    }

    function renderTable(data) {
        var tbody = document.getElementById('amort-table-body');
        if (!tbody) return;
        var rows = data.monthly_savings || [];
        var html = '';
        for (var i = 0; i < rows.length; i++) {
            var r = rows[i];
            html += '<tr>';
            html += '<td>' + r.month + '</td>';
            html += '<td class="num">' + r.days_data + ' / ' + r.days_in_month + '</td>';
            html += '<td class="num">' + r.gen_kwh.toLocaleString('ca') + '</td>';
            html += '<td class="num">' + (r.actual_bill || 0).toFixed(0) + '</td>';
            html += '<td class="num val-red">' + (r.savings_real || 0).toFixed(0) + '</td>';
            html += '<td class="num val-red"><strong>' + (r.cumulative_real || 0).toFixed(0) + '</strong></td>';
            html += '<td class="num val-green">' + r.savings_iber.toFixed(0) + '</td>';
            html += '<td class="num val-blue">' + r.savings_idx.toFixed(0) + '</td>';
            html += '</tr>';
        }
        tbody.innerHTML = html;
    }

    function renderAnalysis(data) {
        var el = document.getElementById('amort-analysis');
        if (!el) return;

        var inv = data.investment || {};
        var months = inv.months_elapsed || 1;
        var cost = inv.cost || 50000;
        var paras = [];

        // Overview
        paras.push(
            'La instal\u00b7laci\u00f3 solar de <strong>' + cost.toLocaleString('ca') + ' \u20ac</strong> ' +
            'porta <strong>' + months + ' mesos</strong> en funcionament des de ' + inv.date + ', ' +
            'amb una generaci\u00f3 total de <strong>' + data.total_gen_kwh.toLocaleString('ca', {maximumFractionDigits:0}) + ' kWh</strong>.'
        );

        // Iberdrola comparison
        var pctIber = data.payback_pct_iber || 0;
        var monthlyIber = data.avg_monthly_savings_iber || 0;
        paras.push(
            'Comparat amb Iberdrola Fix, l\'estalvi acumulat \u00e9s de <strong>' +
            fmt(data.total_savings_iber) + ' \u20ac</strong> (' + pctIber.toFixed(1) + '% de la inversi\u00f3). ' +
            'A un ritme de <strong>' + fmt(monthlyIber) + ' \u20ac/mes</strong>, la data de payback estimada \u00e9s ' +
            '<strong>' + data.projected_payback_date_iber + '</strong>.'
        );

        // Indexed comparison
        var pctIdx = data.payback_pct || 0;
        var monthlyIdx = data.avg_monthly_savings || 0;
        paras.push(
            'Amb la tarifa indexada actual (Som Energia), l\'estalvi solar acumulat \u00e9s de <strong>' +
            fmt(data.total_savings) + ' \u20ac</strong> (' + pctIdx.toFixed(1) + '%). ' +
            'Payback nom\u00e9s solar: <strong>' + data.projected_payback_date + '</strong>.'
        );

        // Real invoices comparison
        var pctReal = data.payback_pct_real || 0;
        var monthlyReal = data.avg_monthly_savings_real || 0;
        paras.push(
            '<strong>Perspectiva real:</strong> comparant amb les factures reals d\'Iberdrola (' +
            (data.iber_real_monthly || 2067).toLocaleString('ca') + ' \u20ac/mes sense solar), ' +
            'l\'estalvi combinat (solar + canvi tarifa) \u00e9s de <strong>' +
            fmt(data.total_savings_real) + ' \u20ac</strong> (' + pctReal.toFixed(1) + '%). ' +
            'A un ritme de <strong>' + fmt(monthlyReal) + ' \u20ac/mes</strong>, payback estimat: <strong>' +
            data.projected_payback_date_real + '</strong>.'
        );

        // Projection
        var remaining = cost - (data.total_savings_real || 0);
        var lifetime = 25;
        var lifetimeSavings = monthlyReal * 12 * lifetime;
        if (remaining > 0 && monthlyIber > 0) {
            paras.push(
                'En ' + lifetime + ' anys de vida \u00fatil, la projecci\u00f3 d\'estalvi brut \u00e9s de <strong>' +
                lifetimeSavings.toLocaleString('ca', {maximumFractionDigits:0}) + ' \u20ac</strong>, ' +
                'amb un benefici net de <strong>' +
                (lifetimeSavings - cost).toLocaleString('ca', {maximumFractionDigits:0}) + ' \u20ac</strong> ' +
                'despr\u00e9s de recuperar la inversi\u00f3.'
            );
        }

        el.innerHTML = paras.map(function(t) {
            return '<p style="font-size:0.92rem;line-height:1.75;margin:0 0 0.6rem;color:var(--text);text-align:justify;">' + t + '</p>';
        }).join('');
    }
})();
