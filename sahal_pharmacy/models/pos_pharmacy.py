# -*- coding: utf-8 -*-
"""Point of sale integration (BRD 5.5, PRD 35-45).

The till already does barcodes, payments, split payments, discounts, returns and
receipts natively. What it does not know is which products are restricted, so this
module ships the pharmacy attributes down to the client and gives the cashier a way
to look up a verified prescription without leaving the POS.

ONE POS SERVES EVERY INDUSTRY ON THIS PLATFORM — a restaurant, a hotel shop, a hardware
counter and a pharmacy all run the same point_of_sale module and share its asset bundle.
So everything here obeys one rule:

    Nothing pharmaceutical reaches a till that is not flagged `is_pharmacy`.

That applies to the fields loaded, the models loaded, and the JavaScript: the client
patch returns immediately on a non-pharmacy till, before it touches anything. It matters
because this module has already broken every POS on the platform once — an unconditional
`_load_pos_data_models` override made pharmacy.prescription load into every session and
crashed the loader (PROD_PENDING_CHANGES section 1).

The one thing NOT gated on the flag is compliance enforcement. Whether a prescription-only
medicine may be sold is a legal question about the PRODUCT, not a preference of the till,
so sale_pos_guard.py checks every order that actually contains restricted products —
otherwise unticking a checkbox would be a bypass. Orders with no pharmacy products exit
that check immediately.
"""

from odoo import models, fields, api, _

# Batches expiring within this window make the till warn the cashier.
POS_EXPIRY_WARN_DAYS = 60


def _is_pharmacy_config(env, config):
    """True only for a till explicitly flagged as a pharmacy point of sale.

    The POS loader passes a recordset to some hooks and a bare id to others
    (product.template gets `config_id`), so both are accepted rather than assumed.
    """
    if not config:
        return False
    if isinstance(config, int):
        config = env['pos.config'].browse(config)
    return bool(config.exists() and config.is_pharmacy)


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    @api.model
    def _load_pos_data_fields(self, config_id):
        """Ship the pharmacy attributes — to pharmacy tills only.

        A restaurant till has no use for `controlled_schedule`, and loading it there is
        both waste on every product record and pharmacy schema leaking into industries
        that never asked for it.
        """
        fields_list = super()._load_pos_data_fields(config_id)
        if not _is_pharmacy_config(self.env, config_id):
            return fields_list
        extra = [
            'is_medicine', 'pharma_type', 'requires_prescription', 'is_controlled',
            'pharma_generic_name', 'brand_name', 'requires_cold_storage',
            'strength',
        ]
        return list(fields_list) + [f for f in extra if f not in fields_list]


class ProductProduct(models.Model):
    _inherit = 'product.product'

    @api.model
    def _load_pos_data_fields(self, config):
        fields_list = super()._load_pos_data_fields(config)
        if not _is_pharmacy_config(self.env, config):
            return fields_list
        extra = ['is_medicine', 'pharma_type', 'requires_prescription', 'is_controlled',
                 'pharma_generic_name', 'brand_name']
        return list(fields_list) + [f for f in extra if f not in fields_list]


class PosConfig(models.Model):
    _inherit = 'pos.config'

    @api.model
    def _load_pos_data_fields(self, config):
        """The client needs the flag itself, or the JS cannot tell which till it is on."""
        return super()._load_pos_data_fields(config) + ['is_pharmacy']

    is_pharmacy = fields.Boolean(
        string='Pharmacy Point of Sale',
        help="Turn on the pharmacy features for this till: restricted-item flags, "
             "prescription lookup, and expiry warnings. Leave off for ordinary retail "
             "or restaurant tills so they don't load pharmacy data.")


class PosSession(models.Model):
    _inherit = 'pos.session'

    @api.model
    def _load_pos_data_models(self, config):
        """Make pharmacy.prescription available, but only to pharmacy tills.

        `config` is the pos.config recordset. Two conditions must both hold:
        - the till is explicitly flagged a pharmacy point of sale, and
        - the user actually has pharmacy access.

        The group check keeps prescriptions out of a session whose user has no
        pharmacy role at all — even if an
        is_pharmacy flag lingered on a config. Loading this model into every POS —
        retail, restaurant, etc. — is both wasteful and (before this guard) fatal.
        """
        data = super()._load_pos_data_models(config)
        if config.is_pharmacy and self.env.user.has_group('sahal_pharmacy.group_pharmacy_cashier'):
            data = data + ['pharmacy.prescription']
        return data


class PharmacyPrescriptionPos(models.Model):
    # pos.load.mixin gives the model _load_pos_data_search_read / _read, which the POS
    # loader calls on every model in _load_pos_data_models. Without it the till aborts
    # with "'pharmacy.prescription' object has no attribute '_load_pos_data_search_read'".
    _name = 'pharmacy.prescription'
    _inherit = ['pharmacy.prescription', 'pos.load.mixin']

    @api.model
    def _load_pos_data_domain(self, data, config):
        """Only scripts that may actually be dispensed today reach the till."""
        return [
            ('state', 'in', ('verified', 'partially_dispensed')),
            '|', ('valid_until', '=', False),
                 ('valid_until', '>=', fields.Date.context_today(self)),
        ]

    @api.model
    def _load_pos_data_fields(self, config):
        return ['id', 'name', 'patient_id', 'doctor_id', 'state',
                'valid_until', 'has_controlled']

    @api.model
    def pos_search_prescriptions(self, partner_id=None, query=None, limit=20):
        """Look up dispensable prescriptions from the POS.

        A separate RPC rather than relying only on preloaded records: a busy pharmacy
        writes prescriptions all day, and a cashier must be able to find one created
        after the session was opened.
        """
        domain = [
            ('state', 'in', ('verified', 'partially_dispensed')),
            '|', ('valid_until', '=', False),
                 ('valid_until', '>=', fields.Date.context_today(self)),
        ]
        if partner_id:
            domain.append(('patient_id', '=', int(partner_id)))
        if query:
            domain += ['|', ('name', 'ilike', query), ('patient_id.name', 'ilike', query)]
        scripts = self.search(domain, limit=int(limit), order='prescription_date desc')
        return [{
            'id': rx.id,
            'name': rx.name,
            'patient': rx.patient_id.display_name,
            'doctor': rx.doctor_id.display_name,
            'valid_until': rx.valid_until and str(rx.valid_until) or '',
            'has_controlled': rx.has_controlled,
            'allergies': rx.patient_id.allergies or '',
            'lines': [{
                'product_id': line.product_id.id,
                'product': line.product_id.display_name,
                'remaining': line.qty_remaining,
            } for line in rx.line_ids if line.qty_remaining > 0],
        } for rx in scripts]

    @api.model
    def pos_expiry_warnings(self, product_ids):
        """Batches of these products expiring soon or already expired.

        Returned to the till so the cashier is told BEFORE taking payment, rather than
        discovering it when the delivery is validated.
        """
        if not product_ids:
            return {}
        horizon = fields.Datetime.add(fields.Datetime.now(), days=POS_EXPIRY_WARN_DAYS)
        quants = self.env['stock.quant'].sudo().search([
            ('product_id', 'in', [int(p) for p in product_ids]),
            ('quantity', '>', 0),
            ('location_id.usage', '=', 'internal'),
            ('lot_id.expiration_date', '!=', False),
            ('lot_id.expiration_date', '<=', horizon),
        ])
        now = fields.Datetime.now()
        result = {}
        for quant in quants:
            entry = result.setdefault(quant.product_id.id, [])
            entry.append({
                'lot': quant.lot_id.name,
                'expiry': str(fields.Date.to_date(quant.lot_id.expiration_date)),
                'quantity': quant.quantity,
                'expired': quant.lot_id.expiration_date < now,
            })
        return result


class PosOrderLine(models.Model):
    _inherit = 'pos.order.line'

    @api.model
    def _load_pos_data_fields(self, config):
        fields_list = super()._load_pos_data_fields(config)
        if not _is_pharmacy_config(self.env, config):
            return fields_list
        return list(fields_list) + ['pharmacy_prescription_line_id']

    pharmacy_prescription_line_id = fields.Many2one(
        'pharmacy.prescription.line', string='Prescription Line', index=True,
        help='Prescription line this sale fulfils, when sold against a script.')
