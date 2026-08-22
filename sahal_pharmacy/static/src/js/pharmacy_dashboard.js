/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { loadBundle } from "@web/core/assets";
import { Component, onWillStart, onWillUnmount, useEffect, useRef, useState } from "@odoo/owl";

/**
 * Pharmacy dashboard.
 *
 * A back-office client action. It has nothing to do with the point of sale and loads in
 * web.assets_backend, NOT in the POS bundle — a dashboard has no business being
 * downloaded by every till on the platform.
 *
 * All figures come from one RPC (pharmacy.dashboard.get_dashboard_data). Charts are
 * optional: if the chart bundle fails to load, the numbers still render, because a
 * pharmacist needs the expiry count far more than they need a doughnut.
 */
const COLORS = {
    retail: "#2563eb",
    invoiced: "#0b8043",
    mix: ["#2563eb", "#0b8043", "#f59e0b", "#8b5cf6", "#ef4444", "#64748b"],
};

export class PharmacyDashboard extends Component {
    static template = "sahal_pharmacy.Dashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.salesCanvas = useRef("salesChart");
        this.mixCanvas = useRef("mixChart");
        this.charts = [];
        this.state = useState({ data: null, months: 12, loading: true, chartsOk: true });

        onWillStart(async () => {
            try {
                await loadBundle("web.chartjs_lib");
            } catch {
                this.state.chartsOk = false;
            }
            await this.load();
        });
        useEffect(() => this.renderCharts(), () => [this.state.data]);
        onWillUnmount(() => this.destroyCharts());
    }

    async load() {
        this.state.loading = true;
        try {
            this.state.data = await this.orm.call("pharmacy.dashboard", "get_dashboard_data", [
                this.state.months,
            ]);
        } finally {
            this.state.loading = false;
        }
    }

    async onPeriod(months) {
        this.state.months = months;
        await this.load();
    }

    get currency() {
        return (this.state.data && this.state.data.currency) || { symbol: "", position: "before", decimals: 2 };
    }

    money(value) {
        const { symbol, position, decimals } = this.currency;
        const amount = (value || 0).toLocaleString(undefined, {
            minimumFractionDigits: decimals,
            maximumFractionDigits: decimals,
        });
        return position === "after" ? `${amount} ${symbol}` : `${symbol}${amount}`;
    }

    num(value) {
        return (value || 0).toLocaleString();
    }

    /** Open the list behind a tile. The server owns the domains, not the template. */
    async open(key) {
        const action = await this.orm.call("pharmacy.dashboard", "action_open", [key]);
        if (action) {
            await this.action.doAction(action);
        }
    }

    async openProduct(id) {
        await this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "product.product",
            res_id: id,
            views: [[false, "form"]],
        });
    }

    destroyCharts() {
        this.charts.forEach((chart) => chart.destroy());
        this.charts = [];
    }

    renderCharts() {
        if (!this.state.chartsOk || !this.state.data || typeof Chart === "undefined") {
            return;
        }
        this.destroyCharts();
        const series = this.state.data.series || [];
        const labels = series.map((s) => s.label);
        const grid = "rgba(128,128,128,0.18)";

        if (this.salesCanvas.el && series.length) {
            this.charts.push(
                new Chart(this.salesCanvas.el, {
                    type: "bar",
                    data: {
                        labels,
                        datasets: [
                            {
                                label: "Till takings",
                                data: series.map((s) => s.retail),
                                backgroundColor: COLORS.retail,
                                borderRadius: 4,
                            },
                            {
                                label: "Invoiced",
                                data: series.map((s) => s.invoiced),
                                backgroundColor: COLORS.invoiced,
                                borderRadius: 4,
                            },
                        ],
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        interaction: { mode: "index", intersect: false },
                        plugins: { legend: { position: "bottom", labels: { usePointStyle: true, boxWidth: 8 } } },
                        scales: {
                            x: { grid: { display: false } },
                            y: { beginAtZero: true, grid: { color: grid } },
                        },
                    },
                })
            );
        }

        const mix = this.state.data.mix || [];
        if (this.mixCanvas.el && mix.length) {
            this.charts.push(
                new Chart(this.mixCanvas.el, {
                    type: "doughnut",
                    data: {
                        labels: mix.map((m) => m.label),
                        datasets: [{ data: mix.map((m) => m.value), backgroundColor: COLORS.mix }],
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: { legend: { position: "bottom", labels: { usePointStyle: true, boxWidth: 8 } } },
                    },
                })
            );
        }
    }
}

registry.category("actions").add("sahal_pharmacy.dashboard", PharmacyDashboard);
