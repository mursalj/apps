/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { PosStore } from "@point_of_sale/app/services/pos_store";
import { AlertDialog, ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { _t } from "@web/core/l10n/translation";

/**
 * Pharmacy rules at the till.
 *
 * This is a convenience layer, NOT the enforcement layer: the browser can always be
 * bypassed, so the authoritative checks live server-side in sale_pos_guard.py, which
 * validates every pos.order on creation. What happens here is telling the cashier
 * early — before payment — instead of letting the sale fail at the end.
 *
 * THIS FILE LOADS INTO EVERY POINT OF SALE ON THE PLATFORM. point_of_sale._assets_pos is
 * one shared bundle: a restaurant, a hotel shop and a hardware counter all download and
 * run this patch on `addLineToOrder`, a core method on the hot path of every sale in
 * every industry.
 *
 * So `pharmacyEnabled()` is the first line of every method here. On a till that is not
 * flagged a pharmacy point of sale, the patch hands straight back to super() and does
 * nothing else — no field reads, no RPCs, no dialogs. A bug in the pharmacy logic can
 * then only break pharmacy tills, which is the whole point.
 */
patch(PosStore.prototype, {
    /** True only on a till configured as a pharmacy point of sale. */
    pharmacyEnabled() {
        return Boolean(this.config?.is_pharmacy);
    },

    /**
     * Intercept adding a product so a restricted medicine cannot be rung up
     * without a verified prescription attached to the order.
     */
    async addLineToOrder(vals, order, opts = {}, configure = true) {
        if (!this.pharmacyEnabled()) {
            return await super.addLineToOrder(...arguments);
        }
        const product = vals.product_id || vals.product_tmpl_id;
        if (product && !(await this.pharmacyCheckProduct(product, order))) {
            return;
        }
        const line = await super.addLineToOrder(...arguments);
        // Warn about near-expiry stock only once the line is actually on the order.
        if (line && product) {
            this.pharmacyWarnExpiry(product);
        }
        return line;
    },

    /** True when the product may be sold on this order. */
    async pharmacyCheckProduct(product, order) {
        if (!this.pharmacyEnabled() || !product.requires_prescription) {
            return true;
        }
        const script = order?.pharmacy_prescription_id;
        if (!script) {
            this.dialog.add(AlertDialog, {
                title: _t("Prescription required"),
                body: _t(
                    "%s is a prescription-only medicine. Attach a verified prescription " +
                        "to this order first (Prescription button), or dispense it from " +
                        "the Pharmacy app.",
                    product.display_name
                ),
            });
            return false;
        }
        if (product.is_controlled) {
            // Controlled drugs are allowed only with an explicit acknowledgement; the
            // server re-checks that the user is a pharmacist regardless.
            return await new Promise((resolve) => {
                this.dialog.add(ConfirmationDialog, {
                    title: _t("Controlled substance"),
                    body: _t(
                        "%s is a controlled substance. It will be written to the " +
                            "controlled drugs register and may only be handed over by a " +
                            "pharmacist. Continue?",
                        product.display_name
                    ),
                    confirm: () => resolve(true),
                    cancel: () => resolve(false),
                });
            });
        }
        return true;
    },

    /** Tell the cashier when the stock behind this product is close to expiring. */
    async pharmacyWarnExpiry(product) {
        if (!this.pharmacyEnabled() || !product.is_medicine) {
            return;
        }
        let warnings = {};
        try {
            warnings = await this.data.call("pharmacy.prescription", "pos_expiry_warnings", [
                [product.id],
            ]);
        } catch {
            return; // never block a sale because the lookup failed
        }
        const batches = warnings[product.id] || [];
        if (!batches.length) {
            return;
        }
        const expired = batches.filter((b) => b.expired);
        const soon = batches.filter((b) => !b.expired);
        const lines = [];
        if (expired.length) {
            lines.push(
                _t("EXPIRED: %s", expired.map((b) => `${b.lot} (${b.expiry})`).join(", "))
            );
        }
        if (soon.length) {
            lines.push(
                _t("Expiring soon: %s", soon.map((b) => `${b.lot} (${b.expiry})`).join(", "))
            );
        }
        this.dialog.add(AlertDialog, {
            title: _t("Check the batch — %s", product.display_name),
            body: lines.join("\n"),
        });
    },

    /** Attach a verified prescription to the current order. */
    async pharmacySelectPrescription() {
        if (!this.pharmacyEnabled()) {
            return;
        }
        const order = this.getOrder();
        if (!order) {
            return;
        }
        const partner = order.getPartner();
        let scripts = [];
        try {
            scripts = await this.data.call(
                "pharmacy.prescription",
                "pos_search_prescriptions",
                [partner ? partner.id : false, null, 20]
            );
        } catch {
            this.dialog.add(AlertDialog, {
                title: _t("Prescriptions"),
                body: _t("Could not load prescriptions. Check the connection and retry."),
            });
            return;
        }
        if (!scripts.length) {
            this.dialog.add(AlertDialog, {
                title: _t("No prescription available"),
                body: partner
                    ? _t(
                          "%s has no verified, unexpired prescription. Create and verify " +
                              "one in the Pharmacy app first.",
                          partner.name
                      )
                    : _t("Select the patient on this order first, then pick their prescription."),
            });
            return;
        }
        // Single match: attach it directly rather than making the cashier pick from
        // a list of one during a queue.
        const chosen = scripts.length === 1 ? scripts[0] : await this.pharmacyPickFrom(scripts);
        if (!chosen) {
            return;
        }
        order.pharmacy_prescription_id = chosen.id;
        if (chosen.allergies) {
            this.dialog.add(AlertDialog, {
                title: _t("Patient allergies — %s", chosen.patient),
                body: chosen.allergies,
            });
        }
    },

    async pharmacyPickFrom(scripts) {
        const list = scripts
            .map((s, i) => `${i + 1}. ${s.name} — ${s.patient}`)
            .join("\n");
        return await new Promise((resolve) => {
            this.dialog.add(ConfirmationDialog, {
                title: _t("Select a prescription"),
                body: _t("Verified prescriptions for this customer:\n%s\n\nThe most recent will be used.", list),
                confirm: () => resolve(scripts[0]),
                cancel: () => resolve(null),
            });
        });
    },
});
