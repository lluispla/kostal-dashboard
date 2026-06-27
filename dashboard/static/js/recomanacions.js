/* =========================================================================
   Recomanacions — Load shifting heatmaps + recommendations
   ========================================================================= */

(function () {
    var DOW_NAMES = ['Dl', 'Dt', 'Dc', 'Dj', 'Dv', 'Ds', 'Dg'];

    function buildHeatmap(tableId, matrix, colorFn) {
        var table = document.getElementById(tableId);
        if (!table) return;
        table.innerHTML = '';

        // Header row with hours 0-23
        var thead = document.createElement('thead');
        var headerRow = document.createElement('tr');
        headerRow.innerHTML = '<th></th>';
        for (var h = 0; h < 24; h++) {
            headerRow.innerHTML += '<th>' + h + '</th>';
        }
        thead.appendChild(headerRow);
        table.appendChild(thead);

        // Find max value for scaling
        var maxVal = 0;
        for (var d = 0; d < 7; d++) {
            for (var hh = 0; hh < 24; hh++) {
                if (matrix[d][hh] > maxVal) maxVal = matrix[d][hh];
            }
        }

        var tbody = document.createElement('tbody');
        for (var dow = 0; dow < 7; dow++) {
            var row = document.createElement('tr');
            row.innerHTML = '<td style="font-weight:600; padding:4px 8px;">' + DOW_NAMES[dow] + '</td>';
            for (var hour = 0; hour < 24; hour++) {
                var val = matrix[dow][hour];
                var intensity = maxVal > 0 ? val / maxVal : 0;
                var bgColor = colorFn(intensity);
                var cell = document.createElement('td');
                cell.style.backgroundColor = bgColor;
                cell.style.textAlign = 'center';
                cell.style.padding = '4px 2px';
                cell.style.fontSize = '0.7rem';
                cell.style.minWidth = '28px';
                cell.title = DOW_NAMES[dow] + ' ' + hour + 'h: ' + val.toFixed(1) + ' kWh';
                if (val > 0) {
                    cell.textContent = val.toFixed(1);
                }
                row.appendChild(cell);
            }
            tbody.appendChild(row);
        }
        table.appendChild(tbody);
    }

    function importColor(intensity) {
        // white -> yellow -> red
        if (intensity <= 0) return 'rgba(255,255,255,1)';
        if (intensity <= 0.5) {
            var r = 255;
            var g = 255;
            var b = Math.round(255 - intensity * 2 * 255);
            return 'rgba(' + r + ',' + g + ',' + b + ',' + (0.3 + intensity * 0.7) + ')';
        }
        var r2 = 255;
        var g2 = Math.round(255 - (intensity - 0.5) * 2 * 200);
        return 'rgba(' + r2 + ',' + g2 + ',0,' + (0.5 + intensity * 0.5) + ')';
    }

    function genColor(intensity) {
        // white -> green
        if (intensity <= 0) return 'rgba(255,255,255,1)';
        var g = Math.round(100 + 155 * intensity);
        return 'rgba(40,' + g + ',69,' + (0.2 + intensity * 0.8) + ')';
    }

    fetch('/api/recomanacions')
        .then(function (r) { return r.json(); })
        .then(function (data) {
            document.getElementById('reco-loading').style.display = 'none';
            document.getElementById('reco-content').style.display = 'block';

            // KPIs
            document.getElementById('reco-total-import').textContent =
                data.total_import_kwh_30d.toFixed(1);
            document.getElementById('reco-shiftable').textContent =
                data.total_shiftable_kwh_30d.toFixed(1);
            document.getElementById('reco-avg-rate').textContent =
                data.avg_rate.toFixed(4);

            // 20% shift savings
            var s20 = data.shift_scenarios.find(function (s) { return s.pct === 20; });
            if (s20) {
                document.getElementById('reco-savings-20').textContent =
                    s20.savings_month.toFixed(2);
            }

            // Heatmaps
            buildHeatmap('heatmap-import', data.heatmap_import, importColor);
            buildHeatmap('heatmap-gen', data.heatmap_gen, genColor);

            // Shift scenarios table
            var stbody = document.getElementById('shift-tbody');
            data.shift_scenarios.forEach(function (s) {
                var tr = document.createElement('tr');
                tr.innerHTML = '<td>' + s.pct + '%</td>'
                    + '<td style="text-align:right;">' + s.kwh_shifted.toFixed(1) + '</td>'
                    + '<td style="text-align:right;">' + s.savings_month.toFixed(2) + '</td>';
                stbody.appendChild(tr);
            });

            // Recommendations
            var list = document.getElementById('reco-list');
            data.recommendations.forEach(function (rec) {
                var li = document.createElement('li');
                li.textContent = rec;
                li.style.marginBottom = '0.5rem';
                li.style.lineHeight = '1.5';
                list.appendChild(li);
            });

            // Analysis
            renderAnalysis(data);
        })
        .catch(function (err) {
            console.warn('Recomanacions fetch failed:', err);
            document.getElementById('reco-loading').textContent =
                'Error carregant les recomanacions.';
        });
    function renderAnalysis(data) {
        var el = document.getElementById('reco-analysis');
        if (!el) return;
        function f(v) { return (v||0).toLocaleString('ca',{minimumFractionDigits:2,maximumFractionDigits:2}); }

        var total = data.total_import_kwh_30d || 0;
        var shiftable = data.total_shiftable_kwh_30d || 0;
        var shiftPct = total > 0 ? (shiftable / total * 100).toFixed(0) : 0;
        var avgRate = data.avg_rate || 0;
        var s20 = (data.shift_scenarios || []).find(function(s) { return s.pct === 20; });
        var s50 = (data.shift_scenarios || []).find(function(s) { return s.pct === 50; });

        var paras = [];

        paras.push(
            'En els \u00faltims 30 dies, l\'empresa ha importat <strong>' + total.toFixed(0) +
            ' kWh</strong> de la xarxa a un preu mitj\u00e0 de <strong>' + avgRate.toFixed(4) +
            ' \u20ac/kWh</strong>. D\'aquests, <strong>' + shiftable.toFixed(0) +
            ' kWh</strong> (' + shiftPct + '%) es van consumir durant hores amb excedent solar disponible ' +
            '\u2014 \u00e9s a dir, es podrien haver alimentat directament del sol despla\u00e7ant la c\u00e0rrega.'
        );

        if (s20 && s50) {
            paras.push(
                'Escenaris d\'estalvi: despla\u00e7ant un 20% d\'aquest consum a hores solars, ' +
                'l\'estalvi seria de <strong>' + f(s20.savings_month) + ' \u20ac/mes</strong>. ' +
                'Amb un 50% de despla\u00e7ament (m\u00e9s ambiciu\u00f3s), arribar\u00eda a <strong>' +
                f(s50.savings_month) + ' \u20ac/mes</strong>.'
            );
        }

        paras.push(
            'Consell pr\u00e0ctic: programar la maquin\u00e0ria pesada, impressores 3D i sistemes de climatitzaci\u00f3 ' +
            'entre les 10h i les 15h (m\u00e0xima producci\u00f3 solar) redueix la depend\u00e8ncia de la xarxa ' +
            'i aprofita l\'energia m\u00e9s barata del dia.'
        );

        el.innerHTML = paras.map(function(t) {
            return '<p style="font-size:0.92rem;line-height:1.75;margin:0 0 0.6rem;color:var(--text);text-align:justify;">' + t + '</p>';
        }).join('');
    }
})();
