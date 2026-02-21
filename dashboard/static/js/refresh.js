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
        if (val == null) return;
        el.textContent = el.hasAttribute('data-fmt')
            ? formatValue(val, el.getAttribute('data-fmt'))
            : val;
    });
}

function formatValue(val, fmt) {
    switch (fmt) {
        case '0':   return Math.round(val).toLocaleString('ca');
        case '1':   return Number(val).toFixed(1);
        case '2':   return Number(val).toFixed(2);
        case '4':   return Number(val).toFixed(4);
        case '5':   return Number(val).toFixed(5);
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
                updatePowerCurve(data.energia.power_curve);
            }
            if (data.energia && data.energia.daily_yield_30d) {
                updateYield30d(data.energia.daily_yield_30d);
            }
            if (data.mercat) {
                updateOmieHourly(data.mercat);
            }

            /* Inverter status badges */
            updateInverterBadge('inv-piko15-badge', data.inversors.piko_15);
            updateInverterBadge('inv-ci50-badge', data.inversors.piko_ci_50);

            /* Timestamp */
            const ts = document.getElementById('last-update');
            if (ts) ts.textContent = data.last_update;
        })
        .catch(err => console.warn('Refresh failed:', err));
}

function updateInverterBadge(id, inv) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = inv.text;
    el.className = 'status-badge ' + statusClass(inv.status);
}

function statusClass(code) {
    if (code === 3 || code === 4) return 'status-ok';
    if (code === 1 || code === 2) return 'status-idle';
    if (code === 5) return 'status-error';
    return 'status-off';
}

setInterval(refreshDashboard, REFRESH_INTERVAL);
