/* =========================================================================
   Tooltips — Explicacions en català per a cada KPI i secció
   Matches elements by data-field attribute (dashboard partials) or by id
   (standalone pages like vehicle_solar, amortitzacio, etc.)
   ========================================================================= */

(function () {
    // Tooltip definitions keyed by data-field value or element id
    var TIPS = {
        // === ECONOMIA ===
        "economia.savings_today": "Valor de l'energia solar consumida directament (autoconsum) que no has hagut de comprar de la xarxa.",
        "economia.injection_income_today": "Ingressos pels excedents solars injectats a la xarxa, compensats al preu OMIE spot de cada hora.",
        "economia.total_benefit_today": "Suma d'autoconsum + compensaci\u00f3 d'excedents. Valor total que la planta solar t'ha generat avui.",
        "economia.monthly_benefit": "Benefici solar acumulat durant el mes (autoconsum + excedents).",
        "economia.import_cost_today": "Cost de l'energia importada de la xarxa avui, amb tarifa indexada Som Energia.",
        "economia.import_cost_month": "Cost acumulat de l'energia importada de la xarxa aquest mes.",
        "economia.avg_indexed_rate": "Preu mitj\u00e0 ponderat per kWh importat avui. Inclou OMIE + peatges + c\u00e0rrecs + marge.",
        "economia.cost_all_in_kwh": "Cost total estimat sense IVA (energia + pot\u00e8ncia + IEE + lloguer + bo social) dividit pels kWh importats de xarxa (calibrats). Comparable a la factura pre-IVA.",
        "economia.diff_import_today": "Difer\u00e8ncia en cost d'importaci\u00f3 avui: positiu = amb Iberdrola hauries pagat m\u00e9s.",
        "economia.diff_import_month": "Difer\u00e8ncia acumulada en cost d'importaci\u00f3 aquest mes respecte Iberdrola.",
        "economia.diff_today": "Difer\u00e8ncia total (autoconsum + excedents) entre Som Indexada i Iberdrola avui.",
        "economia.diff_month": "Difer\u00e8ncia total acumulada aquest mes. Positiu = Som \u00e9s m\u00e9s barata.",
        "economia.bill_som_month": "Factura parcial Som Energia (energia + pot\u00e8ncia + fixes + IEE - compensaci\u00f3). Pre-IVA.",
        "economia.bill_iber_month": "Factura parcial Iberdrola Fix pels mateixos dies. Pre-IVA.",
        "economia.bill_diff_projected": "Difer\u00e8ncia projectada mes complet entre Iberdrola i Som. Positiu = Som m\u00e9s barat.",
        "economia.bill_som_day": "Cost diari mitj\u00e0 de la factura amb Som Energia Indexada (pre-IVA).",
        "economia.bill_som_projected": "Projecci\u00f3 de la factura completa del mes amb Som Energia (pre-IVA).",
        "economia.bill_iber_projected": "Projecci\u00f3 de la factura completa del mes amb Iberdrola Fix (pre-IVA).",
        "economia.flux_credit_eur": "Cr\u00e8dit Flux Solar: valor dels excedents no compensats que es converteixen en cr\u00e8dit (80%).",
        "economia.flux_bill_projected": "Factura projectada aplicant el cr\u00e8dit Flux Solar.",

        // === ENERGIA ===
        "energia.plant_power_w": "Pot\u00e8ncia instant\u00e0nia de la planta solar (PIKO 15 + PIKO CI 50). Varia amb la irradi\u00e0ncia.",
        "energia.consumption_w": "Pot\u00e8ncia total que consumeix l'empresa ara. Inclou maquin\u00e0ria, il\u00b7luminaci\u00f3, climatitzaci\u00f3.",
        "energia.grid_flow_w": "Flux amb la xarxa. Negatiu (verd) = exportant excedent. Positiu (vermell) = importan de la xarxa.",
        "energia.self_consumption_rate": "% de l'energia generada consumida directament. 100% = tot el sol es fa servir.",
        "energia.consumption_today_kwh": "Energia total consumida avui (solar + xarxa).",
        "energia.from_pv_kwh": "Part del consum coberta directament per la planta solar (autoconsum).",
        "energia.from_grid_kwh": "Part del consum importada de la xarxa.",
        "energia.curtailment_kwh": "Energia perduda per sobretensi\u00f3. Quan la tensi\u00f3 supera 253V, els inversors redueixen producci\u00f3.",
        "forecast.total_today_kwh": "Previsi\u00f3 de producci\u00f3 solar per avui basada en dades meteorol\u00f2giques.",
        "forecast.total_tomorrow_kwh": "Previsi\u00f3 de producci\u00f3 solar per dem\u00e0.",
        "lost_production.lost_today_kwh": "Producci\u00f3 perduda avui per talls, sobretensi\u00f3 o problemes als inversors.",
        "lost_production.lost_month_kwh": "Producci\u00f3 perduda acumulada durant el mes.",

        // === MERCAT OMIE ===
        "mercat.omie_eur_mwh": "Preu spot del mercat majorista OMIE. Canvia cada hora. Negatiu = et paguen per consumir.",
        "mercat.current_indexed_real": "Cost real de l'energia indexada ara (OMIE + peatges + c\u00e0rrecs + marge).",
        "mercat.fixed_rate": "Preu fix d'Iberdrola que es fa servir com a refer\u00e8ncia.",
        "mercat.cost_fixed_today": "Quant hauries pagat avui amb tarifa fixa Iberdrola.",
        "mercat.cost_indexed_today": "Quant has pagat avui amb tarifa indexada Som Energia.",
        "mercat.diff_today": "Difer\u00e8ncia entre fixa i indexada avui. Positiu = indexada m\u00e9s barata.",
        "mercat.cost_fixed_month": "Cost acumulat mensual amb tarifa fixa Iberdrola.",
        "mercat.cost_indexed_month": "Cost acumulat mensual amb tarifa indexada Som Energia.",
        "mercat.diff_month": "Difer\u00e8ncia acumulada mensual. Positiu = indexada m\u00e9s barata.",

        // === COMPENSACI\u00d3 ===
        "compensacio.energy_cost": "Cost de l'energia importada aquest mes. La compensaci\u00f3 no pot superar aquest valor.",
        "compensacio.surplus_raw": "Valor brut dels excedents injectats a xarxa (al preu OMIE).",
        "compensacio.compensated": "Excedents efectivament compensats a la factura. Limitats al cost d'energia.",
        "compensacio.wasted": "Excedents que superen el cost d'energia i es perden. Amb Flux Solar es recuperarien.",

        // === PREUS NEGATIUS ===
        "negatius.hours_today": "Hores avui amb preu OMIE negatiu. Exportar en preu negatiu et costa diners.",
        "negatius.hours_month": "Total d'hores amb preu negatiu durant el mes.",
        "negatius.impact_today": "Impacte econ\u00f2mic dels preus negatius avui.",
        "negatius.impact_month": "Impacte acumulat dels preus negatius durant el mes.",

        // === BATERIA ===
        "bateria.config.capacity_kwh": "Capacitat de la bateria virtual configurada per a la simulaci\u00f3.",
        "bateria.avoided_import_kwh": "kWh que la bateria hauria absorbit dels excedents i descarregat en hores cares.",
        "bateria.savings_month": "Estalvi mensual estimat amb bateria: energia emmagatzemada \u00d7 difer\u00e8ncia de preu.",
        "bateria.savings_annual_est": "Projecci\u00f3 anual de l'estalvi amb bateria.",

        // === MAXIMETRE ===
        "maximetre.savings_annual": "Estalvi potencial anual reduint la pot\u00e8ncia contractada als pics reals mesurats.",

        // === REACTIVA ===
        "reactiva.total_penalty_eur": "Penalitzaci\u00f3 estimada per exc\u00e9s d'energia reactiva. Es paga quan cos \u03c6 < 0.95.",

        // === PREVISI\u00d3 FACTURA ===
        "previsio.estalvi_mensual": "Difer\u00e8ncia entre la factura m\u00e9s cara (Iberdrola) i la m\u00e9s barata (Som Indexada).",
        "previsio.estalvi_anual": "Projecci\u00f3 de l'estalvi anual canviant a la millor tarifa.",
        "previsio.mensual.iberdrola.net": "Factura mensual estimada amb Iberdrola Pla Estable (amb IVA).",
        "previsio.mensual.holaluz.net": "Factura mensual estimada amb Holaluz Fix (amb IVA).",
        "previsio.mensual.som_periodes.net": "Factura mensual estimada amb Som Energia Per\u00edodes (amb IVA).",
        "previsio.mensual.som_indexada.net": "Factura mensual estimada amb Som Energia Indexada (amb IVA).",

        // === VEHICLE SOLAR (by id) ===
        "ev-today-charge": "kWh carregats al vehicle des de l'excedent solar. La planta t\u00e9 capacitat sobrant que normalment es perd per throttling.",
        "ev-today-v2h": "kWh disponibles per a la llar via V2H, descomptant conducci\u00f3 i p\u00e8rdues d'efici\u00e8ncia (EV 88% \u00d7 bateria 90%).",
        "ev-today-home-value": "Valor de l'energia lliurada a casa (el\u00e8ctric + aerot\u00e8rmia) al preu de la tarifa dom\u00e8stica.",
        "ev-today-net": "Benefici net = valor llar - compensaci\u00f3 OMIE perduda. Amb OMIE quasi zero, la p\u00e8rdua \u00e9s m\u00ednima.",
        "ev-today-elec": "kWh per al consum el\u00e8ctric nocturn (llums, nevera, electrodom\u00e8stics).",
        "ev-today-aero": "kWh per a l'aerot\u00e8rmia. Cada kWh produeix COP \u00d7 kWh t\u00e8rmics.",
        "ev-today-thermal": "Energia t\u00e8rmica produ\u00efda per l'aerot\u00e8rmia. COP 3 = 1 kWh el\u00e8ctric \u2192 3 kWh calor/fred.",
        "ev-today-lost": "Compensaci\u00f3 OMIE que hauries cobrat exportant en lloc de carregar el vehicle.",
        "ev-month-charge": "Total de kWh carregats al vehicle des de la planta solar durant el mes.",
        "ev-month-v2h": "Total de kWh lliurats a la llar via V2H + bateria durant el mes.",
        "ev-month-savings": "Estalvi total a la factura de casa: electricitat nocturna + aerot\u00e8rmia amb sol de l'empresa.",
        "ev-month-net": "Benefici net mensual descomptant la compensaci\u00f3 OMIE perduda.",
        "ev-month-elec": "Total mensual kWh per al consum el\u00e8ctric nocturn.",
        "ev-month-aero": "Total mensual kWh per a l'aerot\u00e8rmia.",
        "ev-month-thermal": "Total mensual d'energia t\u00e8rmica (COP \u00d7 kWh el\u00e8ctrics aerot\u00e8rmia).",
        "ev-month-lost": "Total mensual compensaci\u00f3 OMIE perduda.",
        "ev-annual-savings": "Projecci\u00f3 estalvi anual a la factura de casa, extrapolant dades del mes actual.",
        "ev-annual-net": "Projecci\u00f3 benefici net anual (estalvi llar - compensaci\u00f3 perduda).",
        "ev-payback": "Mesos per recuperar la inversi\u00f3 del Quasar 2 amb l'estalvi net generat.",
        "ev-coverage": "% del consum nocturn de la llar (el\u00e8ctric + aerot\u00e8rmia) cobert pel V2H solar.",
        "ev-thermal-day": "Mitjana di\u00e0ria d'energia t\u00e8rmica produ\u00efda per l'aerot\u00e8rmia amb el vehicle.",
        "ev-hvac-hours": "Hores per nit que l'aerot\u00e8rmia pot funcionar amb l'energia del vehicle.",
        "ev-season": "Temporada actual. El COP varia entre calefacci\u00f3 (hivern) i refrigeraci\u00f3 (estiu).",

        // === AMORTITZACI\u00d3 (by id) ===
        "amort-cost": "Cost total de la instal\u00b7laci\u00f3 solar.",
        "amort-gen": "Energia total generada des de la instal\u00b7laci\u00f3.",
        "amort-savings-iber": "Estalvi acumulat comparat amb Iberdrola Fix.",
        "amort-pct-iber": "% de la inversi\u00f3 recuperada vs Iberdrola.",
        "amort-monthly-iber": "Estalvi mensual mitj\u00e0 vs Iberdrola.",
        "amort-payback-iber": "Data estimada de payback vs Iberdrola.",
        "amort-savings-idx": "Estalvi acumulat amb tarifa indexada Som Energia.",
        "amort-pct-idx": "% de la inversi\u00f3 recuperada amb Som Indexada.",
        "amort-monthly-idx": "Estalvi mensual mitj\u00e0 amb Som Indexada.",
        "amort-payback-idx": "Data estimada de payback amb Som Indexada.",

        // === RECOMANACIONS (by id) ===
        "reco-total-import": "Total kWh importats de la xarxa en 30 dies.",
        "reco-shiftable": "kWh despla\u00e7ables d'hores cares (P1-P3) a barates (P5-P6).",
        "reco-savings-20": "Estalvi mensual si es despla\u00e7a un 20% del consum d'hores cares a barates.",
        "reco-avg-rate": "Tarifa mitjana ponderada de l'energia importada.",

        // === PREVISI\u00d3 SOLAR (by id) ===
        "kpi-forecast-today": "Producci\u00f3 solar prevista per avui segons el model meteorol\u00f2gic.",
        "kpi-forecast-tomorrow": "Producci\u00f3 solar prevista per dem\u00e0.",
        "kpi-forecast-day3": "Producci\u00f3 solar prevista per passat dem\u00e0.",
        "kpi-accuracy": "Precisi\u00f3 mitjana del model comparant prediccions amb producci\u00f3 real.",

        // === COMPARADOR IBERDROLA (by id) ===
        "iber-avg": "Mitjana mensual real de factures Iberdrola (12 mesos, amb IVA).",
        "iber-som": "Estimaci\u00f3 factura Som Indexada basada en dades reals del mes actual (amb IVA).",
        "iber-savings-month": "Difer\u00e8ncia entre factura mitjana Iberdrola i estimaci\u00f3 Som Indexada.",
        "iber-savings-annual": "Projecci\u00f3 estalvi anual canviant d'Iberdrola a Som + autoconsum solar.",
    };

    function applyTooltip(el, text) {
        var card = el.closest('.kpi-card');
        if (!card) return;
        var label = card.querySelector('.label');
        if (!label || label.title) return; // don't overwrite existing
        label.title = text;
        label.style.cursor = 'help';
        label.style.borderBottom = '1px dotted var(--muted)';
    }

    function applyAll() {
        // 1. Match by data-field attribute (dashboard partials)
        document.querySelectorAll('[data-field]').forEach(function (el) {
            var field = el.getAttribute('data-field');
            if (TIPS[field]) applyTooltip(el, TIPS[field]);
        });

        // 2. Match by id (standalone pages)
        Object.keys(TIPS).forEach(function (key) {
            if (key.indexOf('.') !== -1) return; // skip data-field keys
            var el = document.getElementById(key);
            if (el && TIPS[key]) applyTooltip(el, TIPS[key]);
        });
    }

    // Run on DOM ready + delayed for async content
    function init() {
        applyAll();
        setTimeout(applyAll, 2500);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
