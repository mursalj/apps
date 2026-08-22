# -*- coding: utf-8 -*-
"""Supplier licensing and approval (PRD 19, 20, 64, 82).

A pharmacy may only buy medicines from a licensed, approved supplier, and must be able
to show an inspector which supplier a batch came from. Odoo already models vendors,
purchase orders, vendor pricelists (`product.supplierinfo`) and receipts — none of that
is rebuilt here. What Odoo has no concept of is:

* a **pharmaceutical licence** with an expiry date, on the vendor;
* **approval**: a deliberate, attributable decision that this vendor may supply
  medicines at all, and which products they are approved for;
* what to do when someone raises a purchase order against a vendor who is neither.

The enforcement level is configurable, because "warn" and "block" are both defensible
and the right answer depends on the jurisdiction and how the pharmacy is run.
"""

import logging

from dateutil.relativedelta import relativedelta

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# How many days before a licence lapses the pharmacy wants to hear about it.
DEFAULT_LICENCE_ALERT_DAYS = 60

SUPPLIER_CATEGORIES = [
    ('manufacturer', 'Manufacturer'),
    ('importer', 'Importer'),
    ('distributor', 'Distributor'),
    ('wholesaler', 'Wholesaler'),
    ('agent', 'Agent / Broker'),
]


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # --- pharmaceutical supplier profile (PRD 19) ---------------------------
    is_pharma_supplier = fields.Boolean(
        'Pharmaceutical Supplier',
        help='Supplies medicines, supplements or medical devices to this pharmacy.')
    supplier_category = fields.Selection(SUPPLIER_CATEGORIES, string='Supplier Type')
    pharma_licence_no = fields.Char('Pharmaceutical Licence No.')
    pharma_licence_authority = fields.Char('Issuing Authority')
    pharma_licence_expiry = fields.Date('Licence Expires')
    pharma_licence_state = fields.Selection([
        ('none', 'Not Recorded'),
        ('valid', 'Valid'),
        ('expiring', 'Expiring Soon'),
        ('expired', 'Expired'),
    ], string='Licence Status', compute='_compute_pharma_licence_state', store=True)

    # --- approval (PRD 20) --------------------------------------------------
    is_approved_supplier = fields.Boolean(
        'Approved Supplier', tracking=True,
        help='A deliberate decision that this vendor may supply pharmacy products. '
             'Purchases from an unapproved supplier are warned about or blocked, '
             'depending on the configured enforcement.')
    supplier_approved_by_id = fields.Many2one('res.users', string='Approved By',
                                              readonly=True, copy=False)
    supplier_approved_on = fields.Datetime('Approved On', readonly=True, copy=False)
    supplier_approval_note = fields.Text('Approval Note')

    @api.depends('pharma_licence_expiry')
    def _compute_pharma_licence_state(self):
        today = fields.Date.today()
        horizon = today + relativedelta(days=self._licence_alert_days())
        for partner in self:
            expiry = partner.pharma_licence_expiry
            if not expiry:
                partner.pharma_licence_state = 'none'
            elif expiry < today:
                partner.pharma_licence_state = 'expired'
            elif expiry <= horizon:
                partner.pharma_licence_state = 'expiring'
            else:
                partner.pharma_licence_state = 'valid'

    @api.model
    def _licence_alert_days(self):
        param = self.env['ir.config_parameter'].sudo().get_param(
            'sahal_pharmacy.licence_alert_days', DEFAULT_LICENCE_ALERT_DAYS)
        try:
            return max(int(param), 1)
        except (TypeError, ValueError):
            return DEFAULT_LICENCE_ALERT_DAYS

    def action_approve_supplier(self):
        """Approve this vendor to supply pharmacy products."""
        if not self.env.user.has_group('sahal_pharmacy.group_pharmacy_manager'):
            raise UserError(_(
                "Approving a supplier is a Pharmacy Manager decision — it is the "
                "control that keeps unlicensed medicine out of the dispensary."))
        for partner in self:
            if partner.pharma_licence_state == 'expired':
                raise UserError(_(
                    "%(name)s's pharmaceutical licence expired on %(date)s. Record a "
                    "current licence before approving them.",
                    name=partner.display_name, date=partner.pharma_licence_expiry))
            partner.write({
                'is_approved_supplier': True,
                'is_pharma_supplier': True,
                'supplier_approved_by_id': self.env.user.id,
                'supplier_approved_on': fields.Datetime.now(),
            })
            partner.message_post(body=_(
                "Approved as a pharmaceutical supplier. %s",
                partner.supplier_approval_note or ''))

    def action_revoke_supplier_approval(self):
        if not self.env.user.has_group('sahal_pharmacy.group_pharmacy_manager'):
            raise UserError(_("Only a Pharmacy Manager may revoke supplier approval."))
        for partner in self:
            partner.write({'is_approved_supplier': False})
            partner.message_post(body=_("Supplier approval revoked."))

    @api.model
    def _cron_pharma_licence_alert(self):
        """Warn about supplier licences that have lapsed or are about to.

        A report, not an automatic revocation: a licence renewal that has not reached
        the pharmacy's records yet is a paperwork problem, and blocking a supplier over
        it would stop legitimate deliveries.
        """
        today = fields.Date.today()
        horizon = today + relativedelta(days=self._licence_alert_days())
        at_risk = self.sudo().search([
            ('is_pharma_supplier', '=', True),
            ('pharma_licence_expiry', '!=', False),
            ('pharma_licence_expiry', '<=', horizon),
        ], order='pharma_licence_expiry')
        if not at_risk:
            _logger.info("Pharmacy licence scan: every supplier licence is current.")
            return 0
        for partner in at_risk:
            expired = partner.pharma_licence_expiry < today
            partner.message_post(body=_(
                "Pharmaceutical licence %(state)s on %(date)s.",
                state=_('expired') if expired else _('expires'),
                date=partner.pharma_licence_expiry))
        _logger.warning(
            "Pharmacy licence scan: %s supplier licence(s) expired or expiring: %s",
            len(at_risk), ', '.join(at_risk.mapped('display_name')))
        return len(at_risk)


class ProductSupplierInfo(models.Model):
    """Approval is per product AND per vendor (PRD 20).

    A vendor approved for surgical gloves is not thereby approved for antibiotics.
    Odoo's vendor pricelist line is already the product-vendor pair, so approval hangs
    off it rather than inventing a parallel table.
    """
    _inherit = 'product.supplierinfo'

    is_approved_for_product = fields.Boolean(
        'Approved for This Product', default=True,
        help='Untick to record that this vendor may not supply this particular item, '
             'even though they are an approved supplier in general.')
    approval_reference = fields.Char(
        'Approval Reference',
        help='Registration or authorisation number covering this product.')


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    approved_supplier_ids = fields.Many2many(
        'res.partner', string='Approved Suppliers',
        compute='_compute_approved_suppliers',
        help='Vendors on this product\'s purchase tab who are approved suppliers and '
             'approved for this item.')

    @api.depends('seller_ids.partner_id', 'seller_ids.is_approved_for_product')
    def _compute_approved_suppliers(self):
        for product in self:
            sellers = product.seller_ids.filtered(
                lambda s: s.is_approved_for_product
                and s.partner_id.is_approved_supplier)
            product.approved_supplier_ids = sellers.mapped('partner_id')


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    pharma_supplier_warning = fields.Text(
        'Pharmacy Supplier Warning', compute='_compute_pharma_supplier_warning',
        help='Shown on the order so a buyer sees the problem before confirming, not '
             'as an error after.')

    @api.depends('partner_id', 'order_line.product_id')
    def _compute_pharma_supplier_warning(self):
        for order in self:
            order.pharma_supplier_warning = '\n'.join(order._pharma_supplier_problems())

    def _pharma_supplier_problems(self):
        """Every reason this order should not be placed with this vendor."""
        self.ensure_one()
        partner = self.partner_id
        pharma_lines = self.order_line.filtered(
            lambda line: line.product_id.is_medicine)
        if not pharma_lines or not partner:
            return []

        problems = []
        if not partner.is_approved_supplier:
            problems.append(_(
                "%s is not an approved pharmaceutical supplier.", partner.display_name))
        if partner.pharma_licence_state == 'expired':
            problems.append(_(
                "%(name)s's pharmaceutical licence expired on %(date)s.",
                name=partner.display_name, date=partner.pharma_licence_expiry))
        elif partner.pharma_licence_state == 'none' and partner.is_approved_supplier:
            problems.append(_(
                "No pharmaceutical licence is recorded for %s.", partner.display_name))

        for line in pharma_lines:
            product = line.product_id
            sellers = product.seller_ids.filtered(
                lambda s: s.partner_id == partner
                or s.partner_id == partner.commercial_partner_id)
            if not sellers:
                problems.append(_(
                    "%(vendor)s is not listed as a vendor for %(product)s.",
                    vendor=partner.display_name, product=product.display_name))
            elif not any(s.is_approved_for_product for s in sellers):
                problems.append(_(
                    "%(vendor)s is not approved to supply %(product)s.",
                    vendor=partner.display_name, product=product.display_name))
        return problems

    @api.model
    def _supplier_enforcement(self):
        """'off', 'warn' or 'block' — the pharmacy's own policy."""
        return self.env['ir.config_parameter'].sudo().get_param(
            'sahal_pharmacy.supplier_enforcement', 'warn')

    def button_confirm(self):
        """Check the supplier before an order for medicines becomes a commitment."""
        enforcement = self._supplier_enforcement()
        if enforcement != 'off':
            for order in self:
                problems = order._pharma_supplier_problems()
                if not problems:
                    continue
                if enforcement == 'block':
                    raise UserError(_(
                        "This order cannot be confirmed:\n\n%(problems)s\n\nApprove the "
                        "supplier, or record their licence, before ordering medicines "
                        "from them.",
                        problems='\n'.join('• %s' % p for p in problems)))
                order.message_post(body=_(
                    "<strong>Pharmacy supplier warning</strong><br/>%s",
                    '<br/>'.join('• %s' % p for p in problems)))
                self.env['pharmacy.override'].log(
                    'supplier', '\n'.join(problems), record=order,
                    partner=order.partner_id)
        return super().button_confirm()
