/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { loadBundle } from "@web/core/assets";
import { Component, onWillStart, onWillUnmount, useEffect, useRef, useState } from "@odoo/owl";

const COLORS = { consumption: "#2563eb", revenue: "#0b8043", mix: ["#2563eb", "#0b8043", "#f59e0b", "#8b5cf6"] };

export class UtilityDashboard extends Component {
    static template = "utility_management.Dashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.consumptionCanvas = useRef("consumptionChart");
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
            this.state.data = await this.orm.call("utility.dashboard", "get_dashboard_data", [this.state.months]);
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

    money(v) {
        const { symbol, position, decimals } = this.currency;
        const amount = (v || 0).toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
        return position === "after" ? `${amount} ${symbol}` : `${symbol}${amount}`;
    }

    num(v) {
        return (v || 0).toLocaleString();
    }

    openHighUsage() {
        this.action.doActionButton({ type: "object", resModel: "utility.dashboard", name: "action_open_high_usage", call: true });
    }

    async openReading(id) {
        await this.action.doAction({ type: "ir.actions.act_window", res_model: "utility.meter.reading", res_id: id, views: [[false, "form"]] });
    }

    destroyCharts() {
        this.charts.forEach((c) => c.destroy());
        this.charts = [];
    }

    renderCharts() {
        if (!this.state.chartsOk || !this.state.data || typeof Chart === "undefined") {
            return;
        }
        this.destroyCharts();
        const series = this.state.data.series;
        const labels = series.map((s) => s.label);
        const grid = "rgba(128,128,128,0.18)";

        if (this.consumptionCanvas.el) {
            this.charts.push(new Chart(this.consumptionCanvas.el, {
                type: "bar",
                data: {
                    labels,
                    datasets: [
                        { label: "Consumption", data: series.map((s) => s.consumption), backgroundColor: COLORS.consumption, borderRadius: 4, yAxisID: "y", order: 2 },
                        { label: "Revenue", type: "line", data: series.map((s) => s.revenue), borderColor: COLORS.revenue, backgroundColor: COLORS.revenue, tension: 0.3, pointRadius: 3, fill: false, yAxisID: "y1", order: 1 },
                    ],
                },
                options: {
                    responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
                    plugins: { legend: { position: "bottom", labels: { usePointStyle: true, boxWidth: 8 } } },
                    scales: {
                        x: { grid: { display: false } },
                        y: { beginAtZero: true, position: "left", grid: { color: grid }, title: { display: true, text: "Consumption" } },
                        y1: { beginAtZero: true, position: "right", grid: { display: false }, title: { display: true, text: "Revenue" } },
                    },
                },
            }));
        }
        if (this.mixCanvas.el && this.state.data.meter_mix.length) {
            const mix = this.state.data.meter_mix;
            this.charts.push(new Chart(this.mixCanvas.el, {
                type: "doughnut",
                data: { labels: mix.map((m) => m.label), datasets: [{ data: mix.map((m) => m.value), backgroundColor: COLORS.mix }] },
                options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: "bottom", labels: { usePointStyle: true, boxWidth: 8 } } } },
            }));
        }
    }
}

registry.category("actions").add("utility_management.dashboard", UtilityDashboard);
