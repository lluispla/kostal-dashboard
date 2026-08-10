/* =========================================================================
   Auto-refresh — polls /api/dashboard every 30 s, updates DOM + charts
   ========================================================================= */

const REFRESH_INTERVAL = 30000;

function updateFields(data) {
    document.querySelectorAll('[data-field]').forEach(el => {
        const path = el.getAttribute('data-field').split('.');
        let val = data;
        for (const key of path) {
            if (val == null) return;
            val = val[key];
        }
        if (val == null) {
            // data-nd marks a field the backend may report as unmeasurable.
            // Without it, a null would silently leave the last good value on
            // screen, which is how a stale number outlives the data behind it.
            if (el.hasAttribute('data-nd')) el.textContent = 'n/d';
            return;
        }
        el.textContent = el.hasAttribute('data-fmt')
            ? formatValue(val, el.getAttribute('data-fmt'))
            : val;
    });
}

function formatValue(val, fmt) {
    var n = Number(val);
    switch (fmt) {
        case '0':   return Math.round(n).toLocaleString('ca');
        case '1':   return n.toFixed(1);
        case '2':   return n.toFixed(2);
        case '4':   return n.toFixed(4);
        case '5':   return n.toFixed(5);
        case '+2':  return (n >= 0 ? '+' : '') + n.toFixed(2);
        default:    return val;
    }
}

function updateDiffColors(data) {
    document.querySelectorAll('[data-diff-color]').forEach(el => {
        const span = el.querySelector('[data-field]');
        if (!span) return;
        const path = span.getAttribute('data-field').split('.');
        let val = data;
        for (const key of path) {
            if (val == null) return;
            val = val[key];
        }
        if (val == null) return;
        el.classList.remove('val-green', 'val-red');
        el.classList.add(Number(val) >= 0 ? 'val-green' : 'val-red');
    });
}

function refreshDashboard() {
    fetch('/api/dashboard')
        .then(r => r.json())
        .then(data => {
            /* KPI values */
            updateFields(data);

            /* Dynamic green/red on diff cards */
            updateDiffColors(data);

            /* Charts */
            if (data.energia && data.energia.power_curve) {
                updatePowerCurve(data.energia.power_curve, data.forecast);
            }
            if (data.energia && data.energia.daily_yield_30d) {
                updateYield30d(data.energia.daily_yield_30d);
            }
            if (data.mercat) {
                updateOmieHourly(data.mercat);
            }

            /* Incomplete-data banner + "estimated" marker on the consum card */
            updateOfflineWarning(data.energia && data.energia.offline_inverters);
            updateEstimateMarker(data.energia);

            /* Inverter status badges */
            updateInverterBadge('inv-piko15-badge', data.inversors.piko_15);
            updateInverterBadge('inv-ci50-badge', data.inversors.piko_ci_50);

            /* Overvoltage badges */
            updateOvervoltageBadge('ov-badge-piko15', data.inversors.piko_15);
            updateOvervoltageBadge('ov-badge-ci50', data.inversors.piko_ci_50);

            /* Voltage gauges */
            if (typeof updateGauges === 'function') {
                updateGauges(data.inversors);
            }

            /* Compensació gauge */
            if (data.compensacio && typeof updateCompensacioGauge === 'function') {
                updateCompensacioGauge(data.compensacio);
            }

            /* Negative hours chart */
            if (data.negatius && typeof updateNegHoursChart === 'function') {
                updateNegHoursChart(data.negatius);
            }

            /* Maximetre chart */
            if (data.maximetre && typeof updateMaximetreChart === 'function') {
                updateMaximetreChart(data.maximetre);
            }

            /* Battery chart */
            if (data.bateria && typeof updateBatteryChart === 'function') {
                updateBatteryChart(data.bateria);
            }

            /* Negative price alert badge */
            var negBadge = document.getElementById('neg-price-badge');
            if (negBadge && data.negatius) {
                if (data.negatius.is_negative_now) {
                    negBadge.textContent = 'PREU NEGATIU ARA: ' + data.negatius.current_price_mwh.toFixed(2) + ' \u20ac/MWh';
                    negBadge.className = 'status-badge status-error neg-alert-badge';
                    negBadge.style.display = 'inline-block';
                } else {
                    negBadge.style.display = 'none';
                }
            }

            /* Curtailment KPI color */
            var curtEl = document.querySelector('[data-field="energia.curtailment_kwh"]');
            if (curtEl) {
                var parentVal = curtEl.closest('.value');
                if (parentVal) {
                    parentVal.classList.remove('val-red', 'val-muted');
                    parentVal.classList.add(data.energia.curtailment_kwh > 0 ? 'val-red' : 'val-muted');
                }
            }

            /* Timestamp */
            const ts = document.getElementById('last-update');
            if (ts) ts.textContent = data.last_update;
        })
        .catch(err => console.warn('Refresh failed:', err));
}

function updateOfflineWarning(offline) {
    const el = document.getElementById('offline-warning');
    if (!el) return;
    if (!offline || !offline.length) {
        el.style.display = 'none';
        return;
    }
    const txt = document.getElementById('offline-warning-text');
    if (txt) {
        txt.textContent = offline.join(', ') +
            (offline.length > 1 ? ' no comuniquen. ' : ' no comunica. ') +
            "La seva producció no es pot mesurar, així que el consum surt com a " +
            "n/d i la generació i els percentatges d'avui queden per sota del real.";
    }
    el.style.display = '';
}

function updateEstimateMarker(en) {
    const prefix = document.getElementById('cons-est-prefix');
    const note = document.getElementById('cons-est-note');
    const est = !!(en && en.consumption_estimated);
    if (prefix) prefix.textContent = est ? '~' : '';
    if (note) {
        note.style.display = est ? '' : 'none';
        if (est && en.offline_inverters && en.offline_inverters.length) {
            note.textContent = 'Estimat: inclou la producció modelada del ' +
                en.offline_inverters.join(', ');
        }
    }
}

function updateInverterBadge(id, inv) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = inv.text;
    // Prefer the class the backend resolved: the PIKO 15 and the PIKO CI 50 use
    // different status enums, so a bare code cannot be classified here.
    el.className = 'status-badge ' + (inv.state_class || statusClass(inv.status));
}

function updateOvervoltageBadge(id, inv) {
    const el = document.getElementById(id);
    if (!el) return;
    el.style.display = inv.overvoltage ? 'inline-block' : 'none';
}

function statusClass(code) {
    if (code === 3 || code === 4) return 'status-ok';
    if (code === 1 || code === 2) return 'status-idle';
    if (code === 5) return 'status-error';
    return 'status-off';
}

setInterval(refreshDashboard, REFRESH_INTERVAL);
