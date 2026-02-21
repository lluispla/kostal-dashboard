/* =========================================================================
   Comparador d'Ofertes — Client-side bill engine + UI
   ========================================================================= */

(function () {
    'use strict';

    var PERIODS = ['P1', 'P2', 'P3', 'P4', 'P5', 'P6'];
    var data = window.comparadorData;

    // State
    var offers = data.offers.slice();
    var currentScenario = 'real';
    var pctByPeriod = clone(data.scenarios.real);
    var totalKwh = data.monthly_kwh || 1000;
    var exportKwh = data.monthly_export || 0;
    var billingDays = 30;
    var comparisonChart = null;

    function clone(obj) {
        return JSON.parse(JSON.stringify(obj));
    }

    // -----------------------------------------------------------------------
    // Bill computation — pure function, no DOM
    // -----------------------------------------------------------------------

    function computeBill(offer, kwhByPeriod, expKwh, days, omieByPeriod, regulated, taxes) {
        var energyCost = 0;
        var p, kwh, rate;
        for (var i = 0; i < PERIODS.length; i++) {
            p = PERIODS[i];
            kwh = kwhByPeriod[p] || 0;
            if (offer.type === 'fixed') {
                rate = (offer.energy_eur_kwh ? offer.energy_eur_kwh[p] : 0) || 0;
                rate *= (1 - (offer.discount_energy_pct || 0) / 100);
                energyCost += kwh * rate;
            } else {
                rate = (omieByPeriod[p] || 0)
                    + (regulated.peajes[p] || 0)
                    + (regulated.cargos[p] || 0)
                    + (offer.margin_eur_kwh || 0);
                energyCost += kwh * rate;
            }
        }

        var powerCost = 0;
        for (var j = 0; j < PERIODS.length; j++) {
            p = PERIODS[j];
            var pcharge = (offer.power_charges_eur_kw_day ? offer.power_charges_eur_kw_day[p] : 0) || 0;
            var pkw = (offer.contracted_power_kw ? offer.contracted_power_kw[p] : 69) || 69;
            powerCost += pcharge * pkw * days;
        }

        var base = energyCost + powerCost;
        var elecTax = base * taxes.electricity_tax_pct / 100;
        var fixedCharges = (offer.fixed_charges_eur_day || 0) * days;
        var subtotal = base + elecTax + fixedCharges;
        var iva = subtotal * taxes.iva_pct / 100;
        var total = subtotal + iva;
        var injection = expKwh * (offer.injection_eur_kwh || 0);
        var net = total - injection;

        return {
            energyCost: energyCost,
            powerCost: powerCost,
            elecTax: elecTax,
            fixedCharges: fixedCharges,
            iva: iva,
            total: total,
            injection: injection,
            net: net
        };
    }

    function buildKwhByPeriod() {
        var result = {};
        for (var i = 0; i < PERIODS.length; i++) {
            result[PERIODS[i]] = totalKwh * (pctByPeriod[PERIODS[i]] || 0) / 100;
        }
        return result;
    }

    // -----------------------------------------------------------------------
    // Formatting helpers
    // -----------------------------------------------------------------------

    function fmtEur(v) {
        return v.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, '.') + ' \u20ac';
    }

    function escHtml(s) {
        var div = document.createElement('div');
        div.textContent = s || '';
        return div.innerHTML;
    }

    // -----------------------------------------------------------------------
    // Timeline visualization
    // -----------------------------------------------------------------------

    function renderTimelines() {
        var info = data.period_info;
        var weekday = [
            { p: 'P5', start: 0, end: 8 },
            { p: 'P2', start: 8, end: 10 },
            { p: 'P1', start: 10, end: 14 },
            { p: 'P2', start: 14, end: 18 },
            { p: 'P3', start: 18, end: 22 },
            { p: 'P5', start: 22, end: 24 }
        ];
        var saturday = [
            { p: 'P5', start: 0, end: 8 },
            { p: 'P4', start: 8, end: 18 },
            { p: 'P5', start: 18, end: 24 }
        ];
        var sunday = [
            { p: 'P6', start: 0, end: 24 }
        ];

        function buildBar(segments, containerId) {
            var el = document.getElementById(containerId);
            if (!el) return;
            el.innerHTML = '';
            for (var i = 0; i < segments.length; i++) {
                var seg = segments[i];
                var div = document.createElement('div');
                div.className = 'timeline-segment';
                div.style.width = ((seg.end - seg.start) / 24 * 100).toFixed(1) + '%';
                div.style.background = info[seg.p].color;
                div.innerHTML = '<span class="ts-label">' + seg.p + '</span>'
                    + '<span class="ts-hours">' + seg.start + '-' + seg.end + 'h</span>';
                div.title = info[seg.p].label + ' (' + seg.start + ':00 - ' + seg.end + ':00)';
                el.appendChild(div);
            }
        }

        buildBar(weekday, 'timeline-weekday');
        buildBar(saturday, 'timeline-saturday');
        buildBar(sunday, 'timeline-sunday');

        var legend = document.getElementById('timeline-legend');
        if (!legend) return;
        legend.innerHTML = '';
        for (var i = 0; i < PERIODS.length; i++) {
            var p = PERIODS[i];
            var item = document.createElement('span');
            item.className = 'timeline-legend-item';
            item.innerHTML = '<span class="legend-dot" style="background:' + info[p].color + '"></span>'
                + '<strong>' + p + '</strong> ' + info[p].label
                + ' <span class="legend-detail">(' + info[p].schedule + ', ' + info[p].hours_week + 'h/set)</span>';
            legend.appendChild(item);
        }
    }

    // -----------------------------------------------------------------------
    // Sliders
    // -----------------------------------------------------------------------

    function renderSliders() {
        var container = document.getElementById('sliders-container');
        if (!container) return;
        container.innerHTML = '';
        var isCustom = currentScenario === 'personalitzat';
        var info = data.period_info;

        for (var i = 0; i < PERIODS.length; i++) {
            var p = PERIODS[i];
            var pct = pctByPeriod[p] || 0;
            var kwh = (totalKwh * pct / 100).toFixed(0);
            var div = document.createElement('div');
            div.className = 'slider-group';
            div.innerHTML =
                '<div class="slider-label">'
                + '<span class="legend-dot" style="background:' + info[p].color + '"></span>'
                + '<strong>' + p + '</strong> ' + info[p].label
                + '</div>'
                + '<input type="range" class="period-slider" data-period="' + p + '" '
                + 'min="0" max="100" step="0.1" value="' + pct.toFixed(1) + '"'
                + (isCustom ? '' : ' disabled') + '>'
                + '<span class="slider-value">'
                + '<span class="slider-pct" data-period="' + p + '">' + pct.toFixed(1) + '</span>%'
                + ' <span class="slider-kwh">(' + kwh + ' kWh)</span>'
                + '</span>';
            container.appendChild(div);
        }

        if (isCustom) {
            var sliders = container.querySelectorAll('.period-slider');
            for (var j = 0; j < sliders.length; j++) {
                sliders[j].addEventListener('input', onSliderChange);
            }
        }
    }

    function onSliderChange(e) {
        var changedPeriod = e.target.dataset.period;
        var newVal = parseFloat(e.target.value);
        var oldVal = pctByPeriod[changedPeriod];
        var delta = newVal - oldVal;

        var others = PERIODS.filter(function (p) { return p !== changedPeriod; });
        var othersSum = 0;
        for (var i = 0; i < others.length; i++) othersSum += pctByPeriod[others[i]];

        pctByPeriod[changedPeriod] = newVal;

        if (othersSum > 0) {
            for (var i = 0; i < others.length; i++) {
                var share = pctByPeriod[others[i]] / othersSum;
                pctByPeriod[others[i]] = Math.max(0, pctByPeriod[others[i]] - delta * share);
            }
        }

        // Normalize to 100
        var sum = 0;
        for (var i = 0; i < PERIODS.length; i++) sum += pctByPeriod[PERIODS[i]];
        if (sum > 0) {
            for (var i = 0; i < PERIODS.length; i++) {
                pctByPeriod[PERIODS[i]] = pctByPeriod[PERIODS[i]] / sum * 100;
            }
        }

        updateSliderDisplay();
        recalculate();
    }

    function updateSliderDisplay() {
        for (var i = 0; i < PERIODS.length; i++) {
            var p = PERIODS[i];
            var slider = document.querySelector('.period-slider[data-period="' + p + '"]');
            var pctEl = document.querySelector('.slider-pct[data-period="' + p + '"]');
            if (slider) {
                slider.value = (pctByPeriod[p] || 0).toFixed(1);
                var kwhEl = slider.parentNode.querySelector('.slider-kwh');
                if (kwhEl) kwhEl.textContent = '(' + (totalKwh * (pctByPeriod[p] || 0) / 100).toFixed(0) + ' kWh)';
            }
            if (pctEl) pctEl.textContent = (pctByPeriod[p] || 0).toFixed(1);
        }
    }

    // -----------------------------------------------------------------------
    // Scenario tabs
    // -----------------------------------------------------------------------

    function initScenarioTabs() {
        var tabs = document.querySelectorAll('.scenario-tab');
        for (var i = 0; i < tabs.length; i++) {
            tabs[i].addEventListener('click', function () {
                var allTabs = document.querySelectorAll('.scenario-tab');
                for (var j = 0; j < allTabs.length; j++) allTabs[j].classList.remove('active');
                this.classList.add('active');
                currentScenario = this.dataset.scenario;

                if (currentScenario !== 'personalitzat') {
                    pctByPeriod = clone(data.scenarios[currentScenario]);
                }
                renderSliders();
                recalculate();
            });
        }
    }

    // -----------------------------------------------------------------------
    // Input fields
    // -----------------------------------------------------------------------

    function initInputs() {
        var kwhInput = document.getElementById('input-total-kwh');
        var exportInput = document.getElementById('input-export-kwh');
        var daysInput = document.getElementById('input-days');

        kwhInput.value = Math.round(totalKwh);
        exportInput.value = Math.round(exportKwh);
        daysInput.value = billingDays;

        kwhInput.addEventListener('input', function () {
            totalKwh = parseFloat(this.value) || 0;
            updateSliderDisplay();
            recalculate();
        });
        exportInput.addEventListener('input', function () {
            exportKwh = parseFloat(this.value) || 0;
            recalculate();
        });
        daysInput.addEventListener('input', function () {
            billingDays = parseInt(this.value) || 30;
            recalculate();
        });
    }

    // -----------------------------------------------------------------------
    // Offer cards
    // -----------------------------------------------------------------------

    function renderOfferCards() {
        var grid = document.getElementById('offers-grid');
        if (!grid) return;
        grid.innerHTML = '';

        for (var k = 0; k < offers.length; k++) {
            var offer = offers[k];
            var card = document.createElement('div');
            card.className = 'offer-card' + (offer.is_current ? ' offer-current' : '');
            var typeLabel = offer.type === 'fixed' ? 'Fixa' : 'Indexada';
            var rateInfo = '';
            if (offer.type === 'fixed' && offer.energy_eur_kwh) {
                var rates = [];
                for (var p in offer.energy_eur_kwh) rates.push(offer.energy_eur_kwh[p]);
                var allSame = rates.every(function (r) { return r === rates[0]; });
                if (allSame && rates.length > 0) {
                    rateInfo = (rates[0] * 1000).toFixed(2) + ' \u20ac/MWh';
                } else {
                    rateInfo = 'P1-P6 variable';
                }
                if (offer.discount_energy_pct > 0) {
                    rateInfo += ' (-' + offer.discount_energy_pct + '%)';
                }
            } else {
                rateInfo = 'OMIE + ' + ((offer.margin_eur_kwh || 0) * 1000).toFixed(1) + ' \u20ac/MWh';
            }

            card.innerHTML =
                '<div class="offer-card-header">'
                + '<div>'
                + '<strong>' + escHtml(offer.supplier) + '</strong>'
                + (offer.is_current ? ' <span class="badge-current">Actual</span>' : '')
                + '<br><span class="offer-name">' + escHtml(offer.name) + '</span>'
                + '</div>'
                + '<span class="offer-type-badge offer-type-' + offer.type + '">' + typeLabel + '</span>'
                + '</div>'
                + '<div class="offer-card-rate">' + rateInfo + '</div>'
                + '<div class="offer-card-details">'
                + 'Injecció: ' + ((offer.injection_eur_kwh || 0) * 1000).toFixed(0) + ' \u20ac/MWh'
                + ' &middot; Fixes: ' + (offer.fixed_charges_eur_day || 0).toFixed(3) + ' \u20ac/dia'
                + '</div>'
                + '<div class="offer-card-actions">'
                + '<button class="btn-link btn-edit-offer" data-id="' + offer.id + '">Editar</button>'
                + (offer.is_current ? '' : '<button class="btn-link btn-delete-offer" data-id="' + offer.id + '">Eliminar</button>')
                + '</div>';
            grid.appendChild(card);
        }

        // Bind edit/delete
        var editBtns = grid.querySelectorAll('.btn-edit-offer');
        for (var i = 0; i < editBtns.length; i++) {
            editBtns[i].addEventListener('click', function () {
                var id = this.dataset.id;
                var o = offers.find(function (o) { return o.id === id; });
                if (o) openOfferForm(o);
            });
        }
        var delBtns = grid.querySelectorAll('.btn-delete-offer');
        for (var i = 0; i < delBtns.length; i++) {
            delBtns[i].addEventListener('click', function () {
                deleteOfferApi(this.dataset.id);
            });
        }
    }

    // -----------------------------------------------------------------------
    // Offer form
    // -----------------------------------------------------------------------

    function openOfferForm(offer) {
        var modal = document.getElementById('offer-modal');
        modal.style.display = 'flex';
        document.getElementById('modal-title').textContent = offer ? 'Editar oferta' : 'Nova oferta';
        document.getElementById('form-offer-id').value = offer ? offer.id : '';
        document.getElementById('form-supplier').value = offer ? offer.supplier : '';
        document.getElementById('form-name').value = offer ? offer.name : '';
        document.getElementById('form-type').value = offer ? offer.type : 'fixed';
        document.getElementById('form-discount').value = offer ? (offer.discount_energy_pct || 0) : 0;
        document.getElementById('form-injection').value = offer ? (offer.injection_eur_kwh || 0.05) : 0.05;
        document.getElementById('form-fixed-charges').value = offer ? (offer.fixed_charges_eur_day || 1.659) : 1.659;
        document.getElementById('form-margin').value = offer ? (offer.margin_eur_kwh || 0.005) : 0.005;

        for (var i = 0; i < PERIODS.length; i++) {
            var p = PERIODS[i];
            document.getElementById('form-rate-' + p).value =
                offer && offer.energy_eur_kwh ? (offer.energy_eur_kwh[p] || '') : '';
            document.getElementById('form-power-' + p).value =
                offer && offer.contracted_power_kw ? (offer.contracted_power_kw[p] || 69) : 69;
            document.getElementById('form-pcharge-' + p).value =
                offer && offer.power_charges_eur_kw_day ? (offer.power_charges_eur_kw_day[p] || '') : '';
        }

        // Pre-fill flat rate field if all rates are the same
        if (offer && offer.energy_eur_kwh) {
            var rates = [];
            for (var p in offer.energy_eur_kwh) rates.push(offer.energy_eur_kwh[p]);
            if (rates.length > 0 && rates.every(function (r) { return r === rates[0]; })) {
                document.getElementById('form-flat-rate').value = rates[0];
            }
        }

        toggleFormType();
    }

    function closeOfferForm() {
        document.getElementById('offer-modal').style.display = 'none';
    }

    function toggleFormType() {
        var type = document.getElementById('form-type').value;
        document.getElementById('fixed-rates-section').style.display = type === 'fixed' ? 'block' : 'none';
        document.getElementById('indexed-margin-section').style.display = type === 'indexed' ? 'block' : 'none';
    }

    function collectFormData() {
        var type = document.getElementById('form-type').value;
        var offer = {
            supplier: document.getElementById('form-supplier').value.trim(),
            name: document.getElementById('form-name').value.trim(),
            type: type,
            is_current: false,
            discount_energy_pct: parseFloat(document.getElementById('form-discount').value) || 0,
            injection_eur_kwh: parseFloat(document.getElementById('form-injection').value) || 0,
            fixed_charges_eur_day: parseFloat(document.getElementById('form-fixed-charges').value) || 0,
            contracted_power_kw: {},
            power_charges_eur_kw_day: {}
        };

        if (type === 'fixed') {
            offer.energy_eur_kwh = {};
            offer.margin_eur_kwh = null;
            for (var i = 0; i < PERIODS.length; i++) {
                offer.energy_eur_kwh[PERIODS[i]] =
                    parseFloat(document.getElementById('form-rate-' + PERIODS[i]).value) || 0;
            }
        } else {
            offer.energy_eur_kwh = null;
            offer.margin_eur_kwh = parseFloat(document.getElementById('form-margin').value) || 0;
        }

        for (var j = 0; j < PERIODS.length; j++) {
            var pp = PERIODS[j];
            offer.contracted_power_kw[pp] =
                parseFloat(document.getElementById('form-power-' + pp).value) || 69;
            offer.power_charges_eur_kw_day[pp] =
                parseFloat(document.getElementById('form-pcharge-' + pp).value) || 0;
        }

        // Preserve is_current for existing offers
        var editId = document.getElementById('form-offer-id').value;
        if (editId) {
            var existing = offers.find(function (o) { return o.id === editId; });
            if (existing) offer.is_current = existing.is_current;
        }

        return offer;
    }

    function validateForm() {
        var supplier = document.getElementById('form-supplier').value.trim();
        var name = document.getElementById('form-name').value.trim();
        if (!supplier) { alert('Cal indicar la comercialitzadora'); return false; }
        if (!name) { alert('Cal indicar el nom de la tarifa'); return false; }

        var type = document.getElementById('form-type').value;
        if (type === 'fixed') {
            var hasRate = false;
            for (var i = 0; i < PERIODS.length; i++) {
                if (parseFloat(document.getElementById('form-rate-' + PERIODS[i]).value) > 0) hasRate = true;
            }
            if (!hasRate) { alert('Cal introduir almenys un preu d\'energia (P1-P6)'); return false; }
        }
        return true;
    }

    // -----------------------------------------------------------------------
    // API calls
    // -----------------------------------------------------------------------

    function saveOfferApi(offer) {
        var editId = document.getElementById('form-offer-id').value;
        var method = editId ? 'PUT' : 'POST';
        var url = editId ? '/api/ofertes/' + editId : '/api/ofertes';

        fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(offer)
        })
        .then(function (r) { return r.json(); })
        .then(function (saved) {
            if (editId) {
                offers = offers.map(function (o) { return o.id === editId ? saved : o; });
            } else {
                offers.push(saved);
            }
            closeOfferForm();
            renderOfferCards();
            recalculate();
        });
    }

    function deleteOfferApi(id) {
        if (!confirm('Eliminar aquesta oferta?')) return;
        fetch('/api/ofertes/' + id, { method: 'DELETE' })
        .then(function () {
            offers = offers.filter(function (o) { return o.id !== id; });
            renderOfferCards();
            recalculate();
        });
    }

    // -----------------------------------------------------------------------
    // Comparison chart + table
    // -----------------------------------------------------------------------

    function recalculate() {
        var kwhByPeriod = buildKwhByPeriod();
        var results = [];
        for (var i = 0; i < offers.length; i++) {
            var bill = computeBill(offers[i], kwhByPeriod, exportKwh, billingDays,
                data.omie_by_period, data.regulated, data.taxes);
            results.push({ offer: offers[i], bill: bill });
        }

        // Sort cheapest first
        results.sort(function (a, b) { return a.bill.net - b.bill.net; });

        // Show/hide comparison or "add offer" message
        var noMsg = document.getElementById('no-comparison-msg');
        var resDiv = document.getElementById('comparison-results');
        if (results.length < 2) {
            noMsg.style.display = 'block';
            resDiv.style.display = 'none';
        } else {
            noMsg.style.display = 'none';
            resDiv.style.display = 'block';
            updateChart(results);
            updateTable(results);
        }

        updateSensitivityNote();
    }

    function updateChart(results) {
        var labels = [];
        var values = [];
        var colors = [];
        for (var i = 0; i < results.length; i++) {
            var r = results[i];
            labels.push(r.offer.supplier + ' — ' + r.offer.name);
            values.push(Math.round(r.bill.net * 100) / 100);
            if (r.offer.is_current) {
                colors.push('#0C4DA2');
            } else if (i === 0) {
                colors.push('#28a745');
            } else if (i === results.length - 1) {
                colors.push('#E63946');
            } else {
                colors.push('#6c757d');
            }
        }

        var canvas = document.getElementById('chart-comparison');
        if (comparisonChart) {
            comparisonChart.data.labels = labels;
            comparisonChart.data.datasets[0].data = values;
            comparisonChart.data.datasets[0].backgroundColor = colors;
            comparisonChart.update('none');
        } else {
            comparisonChart = new Chart(canvas, {
                type: 'bar',
                data: {
                    labels: labels,
                    datasets: [{
                        label: 'Cost net mensual',
                        data: values,
                        backgroundColor: colors,
                        borderRadius: 4
                    }]
                },
                options: {
                    indexAxis: 'y',
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: {
                                label: function (ctx) {
                                    return ctx.parsed.x.toFixed(2) + ' \u20ac';
                                }
                            }
                        }
                    },
                    scales: {
                        x: {
                            beginAtZero: true,
                            ticks: {
                                callback: function (v) { return v.toFixed(0) + ' \u20ac'; }
                            }
                        }
                    }
                }
            });
        }

        canvas.parentNode.style.height = Math.max(180, results.length * 55 + 60) + 'px';
        canvas.style.height = '100%';
    }

    function updateTable(results) {
        var thead = document.getElementById('comparison-thead');
        var tbody = document.getElementById('comparison-tbody');
        if (!thead || !tbody) return;

        var winnerId = results.length > 0 ? results[0].offer.id : null;
        var currentResult = null;
        for (var i = 0; i < results.length; i++) {
            if (results[i].offer.is_current) { currentResult = results[i]; break; }
        }

        // Header
        var h = '<tr><th>Concepte</th>';
        for (var i = 0; i < results.length; i++) {
            var r = results[i];
            var cls = r.offer.id === winnerId ? ' class="winner-col"' : '';
            var badge = '';
            if (r.offer.id === winnerId) badge += '<span class="winner-badge">Millor preu</span> ';
            if (r.offer.is_current) badge += '<span class="badge-current">Actual</span> ';
            h += '<th' + cls + '>' + badge
                + escHtml(r.offer.supplier)
                + '<br><small>' + escHtml(r.offer.name) + '</small></th>';
        }
        h += '</tr>';
        thead.innerHTML = h;

        // Rows
        var rows = [
            { key: 'energyCost', label: 'Energia' },
            { key: 'powerCost', label: 'Potència' },
            { key: 'elecTax', label: 'Impost elèctric (5,11%)' },
            { key: 'fixedCharges', label: 'Càrregues fixes' },
            { key: 'iva', label: 'IVA (21%)' },
            { key: 'total', label: 'Total brut' },
            { key: 'injection', label: 'Compensació excedents', negate: true },
            { key: 'net', label: 'TOTAL NET', bold: true }
        ];

        var b = '';
        for (var ri = 0; ri < rows.length; ri++) {
            var row = rows[ri];
            var trCls = row.bold ? ' class="row-total"' : '';
            b += '<tr' + trCls + '><td>' + row.label + '</td>';
            for (var ci = 0; ci < results.length; ci++) {
                var val = results[ci].bill[row.key];
                var display = row.negate ? '\u2212' + val.toFixed(2) : val.toFixed(2);
                var colCls = results[ci].offer.id === winnerId ? ' class="winner-col"' : '';
                b += '<td' + colCls + '>' + display + ' \u20ac</td>';
            }
            b += '</tr>';
        }

        // Annual projection
        b += '<tr class="row-annual"><td>Projecció anual (x12)</td>';
        for (var ci = 0; ci < results.length; ci++) {
            var annual = results[ci].bill.net * 12;
            var colCls = results[ci].offer.id === winnerId ? ' class="winner-col"' : '';
            b += '<td' + colCls + '>' + annual.toFixed(2) + ' \u20ac</td>';
        }
        b += '</tr>';

        // Savings vs current
        if (currentResult) {
            b += '<tr class="row-savings"><td>Estalvi vs actual</td>';
            for (var ci = 0; ci < results.length; ci++) {
                var saving = currentResult.bill.net - results[ci].bill.net;
                var annualSaving = saving * 12;
                var colCls = results[ci].offer.id === winnerId ? ' class="winner-col"' : '';
                var valCls = saving > 0.01 ? 'val-green' : (saving < -0.01 ? 'val-red' : '');
                var sign = saving >= 0 ? '+' : '';
                b += '<td' + colCls + '><span class="' + valCls + '">'
                    + sign + saving.toFixed(2) + ' \u20ac/mes'
                    + '<br>' + sign + annualSaving.toFixed(2) + ' \u20ac/any'
                    + '</span></td>';
            }
            b += '</tr>';
        }

        tbody.innerHTML = b;
    }

    function updateSensitivityNote() {
        var el = document.getElementById('sensitivity-note');
        if (!el) return;
        var a = data.actual;
        var noteHtml;
        if (data.low_data) {
            noteHtml = '<strong>Dades limitades:</strong> '
                + 'Només ' + a.hours_data + ' hores de dades reals disponibles ('
                + a.total_kwh.toFixed(0) + ' kWh importats, '
                + a.export_kwh.toFixed(0) + ' kWh exportats). '
                + 'Els valors de consum mensual s\'han d\'ajustar manualment per obtenir resultats fiables.';
        } else {
            noteHtml = 'Basat en <strong>' + a.days.toFixed(0) + ' dies</strong> de dades reals '
                + '(' + a.total_kwh.toLocaleString('ca') + ' kWh importats, '
                + a.export_kwh.toLocaleString('ca') + ' kWh exportats). '
                + 'Preus OMIE mitjans per període dels últims ' + a.months_analysed + ' mesos.';
        }
        el.innerHTML = noteHtml;
    }

    // -----------------------------------------------------------------------
    // Form event bindings
    // -----------------------------------------------------------------------

    function initFormBindings() {
        document.getElementById('btn-add-offer').addEventListener('click', function () {
            openOfferForm(null);
        });
        document.getElementById('btn-cancel-offer').addEventListener('click', closeOfferForm);
        document.getElementById('btn-save-offer').addEventListener('click', function () {
            if (!validateForm()) return;
            saveOfferApi(collectFormData());
        });
        document.getElementById('form-type').addEventListener('change', toggleFormType);
        document.getElementById('btn-apply-flat').addEventListener('click', function (e) {
            e.preventDefault();
            var rate = document.getElementById('form-flat-rate').value;
            if (rate) {
                for (var i = 0; i < PERIODS.length; i++) {
                    document.getElementById('form-rate-' + PERIODS[i]).value = rate;
                }
            }
        });

        // Close modal on backdrop click
        document.getElementById('offer-modal').addEventListener('click', function (e) {
            if (e.target === this) closeOfferForm();
        });

        // Close modal on Escape
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') closeOfferForm();
        });
    }

    // -----------------------------------------------------------------------
    // Initialize
    // -----------------------------------------------------------------------

    function init() {
        renderTimelines();
        initScenarioTabs();
        initInputs();
        renderSliders();
        renderOfferCards();
        initFormBindings();
        recalculate();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
