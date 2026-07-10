/* =========================================================================
   Vehicle Solar — EV solar charging + V2H + Aerotermia simulation
   ========================================================================= */

(function () {
    var COLORS = {
        green: '#28a745',
        blue: '#0C4DA2',
        navy: '#002B5B',
        orange: '#fd7e14',
        red: '#E63946',
        grey: '#6c757d',
    };

    // Season → HVAC badge color
    var SEASON_COLORS = {
        heating: '#E63946',   // red/warm
        cooling: '#0C4DA2',   // blue/cool
        transition: '#6c757d',
    };

    fetch('/api/vehicle-solar')
        .then(function (r) { return r.json(); })
        .then(function (data) {
            document.getElementById('ev-loading').style.display = 'none';
            if (!data.enabled) {
                document.getElementById('ev-disabled').style.display = 'block';
                return;
            }
            document.getElementById('ev-content').style.display = 'block';
            renderAll(data);
        })
        .catch(function (err) {
            document.getElementById('ev-loading').textContent =
                'Error carregant dades: ' + err.message;
        });

    function fmt(v) {
        return v.toLocaleString('ca', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    function setNet(el, v, unit) {
        el.className = 'value ' + (v >= 0 ? 'val-green' : 'val-red');
        el.innerHTML = fmt(v) + ' <span class="unit">' + unit + '</span>';
    }

    function renderAll(data) {
        var season = data.season;
        var isHeating = season === 'heating';
        var isCooling = season === 'cooling';
        var hasHVAC = isHeating || isCooling;

        // HVAC badge styling
        var badge = document.getElementById('ev-hvac-badge');
        badge.style.background = SEASON_COLORS[season] || SEASON_COLORS.transition;
        if (isHeating) {
            badge.innerHTML = '&#128293; Calefacci\u00f3';
        } else if (isCooling) {
            badge.innerHTML = '&#10052; Refrigeraci\u00f3';
        } else {
            badge.innerHTML = '&#127777; Transici\u00f3';
        }

        // Model title
        document.getElementById('ev-model-title').textContent =
            data.model + ' \u2014 Solar a Casa';

        // Season labels for thermal fields
        var thermalIcon = isHeating ? '&#128293;' : (isCooling ? '&#10052;' : '&#127777;');
        var thermalWord = isHeating ? 'calefacci\u00f3' : (isCooling ? 'refrigeraci\u00f3' : 't\u00e8rmica');
        var aeroWord = isHeating ? 'Calefacci\u00f3' : (isCooling ? 'Refrigeraci\u00f3' : 'Aerot\u00e8rmia');

        // Update labels dynamically
        document.getElementById('ev-today-aero-label').innerHTML = thermalIcon + ' ' + aeroWord;
        document.getElementById('ev-today-thermal-label').innerHTML = thermalIcon + ' Energia ' + thermalWord;
        document.getElementById('ev-month-aero-label').innerHTML = thermalIcon + ' ' + aeroWord + ' total';
        document.getElementById('ev-month-thermal-label').innerHTML = thermalIcon + ' Energia ' + thermalWord + ' total';
        document.getElementById('ev-thermal-day-label').innerHTML = thermalIcon + ' ' + aeroWord + ' mitjana/dia';
        document.getElementById('ev-hvac-hours-label').innerHTML = thermalIcon + ' Hores ' + aeroWord.toLowerCase() + '/nit';

        // Today KPIs
        var t = data.today;
        document.getElementById('ev-today-charge').innerHTML =
            fmt(t.solar_charge_kwh) + ' <span class="unit">kWh</span>';
        document.getElementById('ev-today-v2h').innerHTML =
            fmt(t.available_v2h_kwh) + ' <span class="unit">kWh</span>';
        document.getElementById('ev-today-home-value').innerHTML =
            fmt(t.home_value_eur) + ' <span class="unit">\u20ac</span>';
        setNet(document.getElementById('ev-today-net'), t.net_benefit_eur, '\u20ac');

        // Today split
        document.getElementById('ev-today-elec').innerHTML =
            fmt(t.elec_kwh) + ' <span class="unit">kWh</span>';
        document.getElementById('ev-today-aero').innerHTML =
            fmt(t.aero_elec_kwh) + ' <span class="unit">kWh</span>';
        var thermalEl = document.getElementById('ev-today-thermal');
        thermalEl.innerHTML = fmt(t.thermal_kwh) + ' <span class="unit">kWh</span>';
        thermalEl.style.color = SEASON_COLORS[season] || SEASON_COLORS.transition;
        document.getElementById('ev-today-lost').innerHTML =
            fmt(t.lost_compensation_eur) + ' <span class="unit">\u20ac</span>';

        // Monthly KPIs
        var m = data.month;
        document.getElementById('ev-month-charge').innerHTML =
            fmt(m.solar_charge_kwh) + ' <span class="unit">kWh</span>';
        document.getElementById('ev-month-v2h').innerHTML =
            fmt(m.v2h_kwh) + ' <span class="unit">kWh</span>';
        document.getElementById('ev-month-savings').innerHTML =
            fmt(m.home_savings_eur) + ' <span class="unit">\u20ac</span>';
        setNet(document.getElementById('ev-month-net'), m.net_benefit_eur, '\u20ac');
        document.getElementById('ev-month-days').textContent = m.days + ' dies amb dades';

        // Monthly split
        document.getElementById('ev-month-elec').innerHTML =
            fmt(m.elec_kwh) + ' <span class="unit">kWh</span>';
        document.getElementById('ev-month-aero').innerHTML =
            fmt(m.aero_elec_kwh) + ' <span class="unit">kWh</span>';
        var mThermalEl = document.getElementById('ev-month-thermal');
        mThermalEl.innerHTML = fmt(m.thermal_kwh) + ' <span class="unit">kWh</span>';
        mThermalEl.style.color = SEASON_COLORS[season] || SEASON_COLORS.transition;
        document.getElementById('ev-month-lost').innerHTML =
            fmt(m.lost_compensation_eur) + ' <span class="unit">\u20ac</span>';

        // LCOE \u2014 cost real del kWh solar carregat
        var l = data.lcoe;
        if (l) {
            var fmt4 = function (v) {
                return v.toLocaleString('ca', { minimumFractionDigits: 4, maximumFractionDigits: 4 });
            };
            document.getElementById('ev-lcoe-rate').innerHTML =
                fmt4(l.lcoe_lifetime) + ' <span class="unit">\u20ac/kWh</span>';
            document.getElementById('ev-lcoe-basis').textContent = l.based_on_real
                ? 'producci\u00f3 real (' + Math.round(l.annual_kwh_proj).toLocaleString('ca') + ' kWh/any)'
                : 'estim. disseny (poques dades encara)';
            document.getElementById('ev-lcoe-month-cost').innerHTML =
                fmt(m.charge_cost_lcoe_eur) + ' <span class="unit">\u20ac</span>';
            document.getElementById('ev-lcoe-month-kwh').textContent =
                fmt(m.solar_charge_kwh) + ' kWh carregats';
            document.getElementById('ev-lcoe-annual-cost').innerHTML =
                fmt(data.projection.annual_charge_cost_lcoe_eur) + ' <span class="unit">\u20ac/any</span>';
            document.getElementById('ev-lcoe-annual-kwh').textContent =
                Math.round(data.projection.annual_charge_kwh).toLocaleString('ca') + ' kWh/any projectats';
            var vsGrid = (data.projection.annual_charge_cost_grid_eur || 0) - (data.projection.annual_charge_cost_lcoe_eur || 0);
            document.getElementById('ev-lcoe-vs-grid').innerHTML =
                fmt(vsGrid) + ' <span class="unit">\u20ac/any</span>';
            document.getElementById('ev-lcoe-vs-grid-note').textContent =
                'vs comprar a ' + fmt4(data.home_rate_eur_kwh || 0.17) + ' \u20ac/kWh';
        }

        // Projection KPIs
        var p = data.projection;
        document.getElementById('ev-annual-savings').innerHTML =
            fmt(p.annual_home_savings_eur) + ' <span class="unit">\u20ac/any</span>';
        setNet(document.getElementById('ev-annual-net'), p.annual_net_benefit_eur, '\u20ac/any');
        document.getElementById('ev-payback').innerHTML =
            p.charger_payback_months < 900
                ? fmt(p.charger_payback_months) + ' <span class="unit">mesos</span>'
                : 'N/A';
        document.getElementById('ev-coverage').innerHTML =
            p.home_coverage_pct.toFixed(1) + ' <span class="unit">%</span>';

        // Thermal projection
        var tdEl = document.getElementById('ev-thermal-day');
        tdEl.innerHTML = fmt(p.avg_thermal_kwh_day) + ' <span class="unit">kWh</span>';
        tdEl.style.color = SEASON_COLORS[season] || SEASON_COLORS.transition;
        document.getElementById('ev-hvac-hours').innerHTML =
            p.hvac_hours_night.toFixed(1) + ' <span class="unit">h</span>';

        // Season info
        document.getElementById('ev-season').textContent = data.season_label;
        document.getElementById('ev-cop-info').textContent =
            hasHVAC ? 'COP ' + data.cop + ' \u2014 1 kWh el\u00e8ctric = ' + data.cop + ' kWh t\u00e8rmics' : 'Sense demanda HVAC';

        // Dynamic analysis
        renderAnalysis(data);

        // Charts
        renderHourlyChart(t.hourly_chart);
        renderDailyChart(data.daily_chart, season);
    }

    function renderAnalysis(data) {
        var el = document.getElementById('ev-analysis');
        if (!el) return;

        var m = data.month;
        var p = data.projection;
        var t = data.today;
        var season = data.season;
        var cop = data.cop;
        var days = m.days || 1;

        var dailyCharge = m.solar_charge_kwh / days;
        var dailyV2H = m.v2h_kwh / days;
        var dailyElec = m.elec_kwh / days;
        var dailyAero = m.aero_elec_kwh / days;
        var dailyThermal = m.thermal_kwh / days;
        var chainEff = 0.792; // 88% * 90%
        var lostPerKwh = m.solar_charge_kwh > 0 ? m.lost_compensation_eur / m.solar_charge_kwh : 0;

        // Month name in Catalan
        var monthNames = ['gener','febrer','mar\u00e7','abril','maig','juny',
                          'juliol','agost','setembre','octubre','novembre','desembre'];
        var now = new Date();
        var monthName = monthNames[now.getMonth()] || '';
        var year = now.getFullYear();

        // Build paragraphs
        var paras = [];

        // P1: Plant capacity and charging
        paras.push(
            'La planta de 65 kWp, actualment limitada per throttling (no legalitzada), disposa de capacitat sobrant ' +
            'per carregar una mitjana de <strong>' + dailyCharge.toFixed(0) + ' kWh/dia</strong> al ' + data.model + '. ' +
            'Despr\u00e9s de descomptar 1,3 kWh de conducci\u00f3 i un 21% de p\u00e8rdues en la cadena ' +
            '(EV 88% \u00d7 bateria llar 90%), arriben <strong>' + dailyV2H.toFixed(0) + ' kWh/dia</strong> a la llar.'
        );

        // P2: Energy split
        if (season === 'heating' || season === 'cooling') {
            var hvacWord = season === 'heating' ? 'calefacci\u00f3 radiant' : 'refrigeraci\u00f3';
            var hvacIcon = season === 'heating' ? '\ud83d\udd25' : '\u2744\ufe0f';
            paras.push(
                'Aquesta energia es reparteix entre <strong>' + dailyElec.toFixed(0) + ' kWh</strong> per al consum ' +
                'el\u00e8ctric nocturn i <strong>' + dailyAero.toFixed(0) + ' kWh</strong> per a l\'aerot\u00e8rmia, ' +
                'que amb un COP de ' + cop + ' produeix ' + hvacIcon + ' <strong>' + dailyThermal.toFixed(0) +
                ' kWh t\u00e8rmics diaris</strong> de ' + hvacWord +
                ' \u2014 aproximadament <strong>' + p.hvac_hours_night.toFixed(1) + ' hores per nit</strong>.'
            );
        } else {
            paras.push(
                'En \u00e8poca de transici\u00f3 (sense demanda de calefacci\u00f3 ni refrigeraci\u00f3), ' +
                'tota l\'energia es destina al consum el\u00e8ctric nocturn: ' +
                '<strong>' + dailyElec.toFixed(0) + ' kWh/nit</strong>.'
            );
        }

        // P3: Opportunity cost
        paras.push(
            'El cost d\'oportunitat \u00e9s negligible: el preu OMIE durant les hores solars \u00e9s pr\u00e0cticament ' +
            'zero (<strong>' + (lostPerKwh * 100).toFixed(1) + ' c\u00e8ntims/kWh</strong> de mitjana). ' +
            'En ' + days + ' dies de ' + monthName + ', la compensaci\u00f3 perduda \u00e9s de nom\u00e9s ' +
            '<strong>' + fmt(m.lost_compensation_eur) + ' \u20ac</strong>, ' +
            'mentre que l\'estalvi a la llar \u00e9s de <strong>' + fmt(m.home_savings_eur) + ' \u20ac</strong>.'
        );

        // P4: Projection
        var paybackYears = p.charger_payback_months / 12;
        paras.push(
            'Projecci\u00f3 anual: <strong>' + fmt(p.annual_net_benefit_eur) + ' \u20ac/any</strong> de benefici net, ' +
            'amb un payback del Quasar 2 de <strong>~' + paybackYears.toFixed(1) + ' anys</strong> i una cobertura ' +
            'energ\u00e8tica nocturna del <strong>' + p.home_coverage_pct.toFixed(0) + '%</strong>.'
        );

        // P5: Seasonal outlook
        if (season === 'heating') {
            paras.push(
                'Nota: aquestes dades s\u00f3n de ' + monthName + ' ' + year + ' (hivern, menys hores de sol). ' +
                'A l\'estiu, amb m\u00e9s irradiaci\u00f3 i demanda de refrigeraci\u00f3, ' +
                'els n\u00fameros milloraran significativament.'
            );
        } else if (season === 'cooling') {
            paras.push(
                'L\'estiu \u00e9s l\'\u00e8poca \u00f2ptima: m\u00e0xima producci\u00f3 solar combinada amb ' +
                'demanda de refrigeraci\u00f3. El COP de ' + cop + ' multiplica cada kWh solar.'
            );
        }

        el.innerHTML = paras.join('</p><p style="font-size:0.92rem;line-height:1.75;margin:0.6rem 0 0;color:var(--text);text-align:justify;">');
    }

    function renderHourlyChart(hourly) {
        var canvas = document.getElementById('chart-ev-hourly');
        if (!canvas || !hourly || hourly.length === 0) return;

        var labels = hourly.map(function (d) { return d.x; });

        new Chart(canvas, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [
                    {
                        label: 'Potencial solar (kW)',
                        data: hourly.map(function (d) { return d.potential || 0; }),
                        type: 'line',
                        borderColor: COLORS.orange,
                        borderWidth: 2,
                        borderDash: [6, 3],
                        pointRadius: 2,
                        fill: false,
                        order: 0,
                    },
                    {
                        label: 'Consum empresa (kW)',
                        data: hourly.map(function (d) { return d.load || 0; }),
                        type: 'line',
                        borderColor: COLORS.grey,
                        borderWidth: 2,
                        pointRadius: 2,
                        fill: false,
                        order: 1,
                    },
                    {
                        label: 'Carregat al EV (kWh)',
                        data: hourly.map(function (d) { return d.charged; }),
                        backgroundColor: COLORS.blue + 'cc',
                        borderColor: COLORS.blue,
                        borderWidth: 1,
                        borderRadius: 3,
                        order: 2,
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
                                var unit = ctx.datasetIndex < 2 ? ' kW' : ' kWh';
                                return ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(1) + unit;
                            }
                        }
                    }
                },
                scales: {
                    x: { grid: { display: false }, ticks: { color: '#6c757d' } },
                    y: {
                        title: { display: true, text: 'kW / kWh' },
                        ticks: { color: '#6c757d' },
                        grid: { color: '#f0f0f0' },
                    },
                },
            }
        });
    }

    function renderDailyChart(daily, season) {
        var canvas = document.getElementById('chart-ev-daily');
        if (!canvas || !daily || daily.length === 0) return;

        var labels = daily.map(function (d) { return d.x; });

        // Cumulative net benefit
        var cumNet = [];
        var running = 0;
        for (var i = 0; i < daily.length; i++) {
            running += daily[i].net;
            cumNet.push({ x: daily[i].x, y: Math.round(running * 100) / 100 });
        }

        var thermalColor = SEASON_COLORS[season] || SEASON_COLORS.transition;

        new Chart(canvas, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [
                    {
                        label: 'El\u00e8ctric nit (kWh)',
                        data: daily.map(function (d) { return d.elec; }),
                        backgroundColor: COLORS.navy + 'bb',
                        borderRadius: 2,
                        stack: 'energy',
                        order: 3,
                        yAxisID: 'y',
                    },
                    {
                        label: 'Aerot\u00e8rmia (kWh)',
                        data: daily.map(function (d) { return d.aero; }),
                        backgroundColor: thermalColor + '99',
                        borderRadius: 2,
                        stack: 'energy',
                        order: 2,
                        yAxisID: 'y',
                    },
                    {
                        label: 'Benefici net acumulat (\u20ac)',
                        data: cumNet,
                        type: 'line',
                        borderColor: COLORS.green,
                        borderWidth: 2.5,
                        pointRadius: 3,
                        fill: false,
                        order: 1,
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
                                var unit = ctx.datasetIndex < 2 ? ' kWh' : ' \u20ac';
                                return ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(2) + unit;
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        grid: { display: false },
                        ticks: { color: '#6c757d', maxRotation: 45 },
                        stacked: true,
                    },
                    y: {
                        position: 'left',
                        title: { display: true, text: 'kWh lliurats a la llar' },
                        ticks: { color: '#6c757d' },
                        grid: { color: '#f0f0f0' },
                        stacked: true,
                    },
                    y1: {
                        position: 'right',
                        title: { display: true, text: 'Benefici net acumulat (\u20ac)' },
                        ticks: { color: '#6c757d', callback: function (v) { return v.toFixed(2) + ' \u20ac'; } },
                        grid: { display: false },
                    },
                },
            }
        });
    }
})();
