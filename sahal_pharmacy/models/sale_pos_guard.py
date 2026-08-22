# -*- coding: utf-8 -*-
"""Prescription validation at the point of sale (BRD 5.5, 5.7).

The catalogue can flag a medicine prescription-only, but a flag that nothing enforces
is decoration. These guards make it real on both selling channels:

  * back office / e-commerce -> sale.order.action_confirm
  * shop counter             -> pos.order at creation

A sale raised BY a dispensing is exempt: the prescription was already verified by a
pharmacist, and the dispensing itself performed the controlled-drug and expiry checks.

Strictness is configurable (sahal_pharmacy.enforce_prescription, default on) because a
wholesale pharmacy selling to licensed retailers has a different legal position from a
retail counter selling to the public. Turning it off is a deliberate, recorded choice
rather than an accident.
"""

import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PharmacyPrescriptionGuardMixin(models.AbstractModel):
    """Shared enforcement so the two channels cannot drift apart."""
    _name = 'pharmacy.prescription.guard'
    _description = 'Prescription Enforcement Helper'

    @api.model
    def _enforcement_enabled(self):
        param = self.env['ir.config_parameter'].sudo().get_param(
            'sahal_pharmacy.enforce_prescription', 'True')
        return str(param).strip().lower() not in ('false', '0', 'no', 'off')

    @api.model
    def _restricted_products(self, products):
        """Split products into (prescription-only, controlled)."""
        rx_only = products.filtered('requires_prescription')
        controlled = products.filtered('is_controlled')
        return rx_only, controlled

    @api.model
    def _check_products_sellable(self, products, prescription, document_name,
                                 wholesale_partner=None):
        """Raise unless every restricted product is covered by a verified prescription.

        `wholesale_partner` changes only the WORDING, never the outcome. A wholesaler
        whose customer is not approved must still be stopped, but telling them to
        "dispense it from a verified prescription" points at the retail counter, which
        is not where a bulk order to a hospital gets fixed. They need to hear about the
        licence.
        """
        if not self._enforcement_enabled():
            return
        rx_only, controlled = self._restricted_products(products)
        if not rx_only:
            return

        if not prescription:
            if wholesale_partner is not None:
                raise UserError(_(
                    "%(doc)s supplies prescription-only medicine (%(items)s) to "
                    "%(partner)s, who is not approved to buy it wholesale or whose "
                    "trading licence is not valid. Approve the account and record a "
                    "current licence, or dispense against a prescription instead.",
                    doc=document_name, partner=wholesale_partner.display_name,
                    items=', '.join(rx_only.mapped('display_name')[:5])))
            raise UserError(_(
                "%(doc)s contains prescription-only medicine (%(items)s). Dispense it "
                "from a verified prescription in the Pharmacy app instead of selling it "
                "directly.",
                doc=document_name,
                items=', '.join(rx_only.mapped('display_name')[:5])))

        if prescription.state not in ('verified', 'partially_dispensed', 'dispensed'):
            raise UserError(_(
                "Prescription %(rx)s has not been verified by a pharmacist, so "
                "%(doc)s cannot be completed.", rx=prescription.name, doc=document_name))
        if prescription.is_expired:
            raise UserError(_(
                "Prescription %(rx)s expired on %(date)s.",
                rx=prescription.name, date=prescription.valid_until))

        # Controlled substances never leave on a bare till receipt.
        if controlled and not self.env.user.has_group(
                'sahal_pharmacy.group_pharmacy_pharmacist'):
            raise UserError(_(
                "%(doc)s contains a controlled substance (%(items)s), which only a "
                "pharmacist may hand over.",
                doc=document_name,
                items=', '.join(controlled.mapped('display_name')[:5])))


class SaleOrder(models.Model):
    _name = 'sale.order'
    _inherit = ['sale.order']

    pharmacy_prescription_id = fields.Many2one(
        'pharmacy.prescription', string='Prescription', copy=False, index=True,
        help='Prescription this order dispenses against. Set automatically when the '
             'order is raised from the Pharmacy app.')

    def _is_licensed_wholesale_order(self):
        """True when this order is bulk supply to an approved, licensed business.

        Deliberately narrow: being marked wholesale is not enough. The customer must be
        APPROVED to buy medicines and hold a licence that has not expired - exactly the
        conditions the wholesale check enforces. An unapproved or lapsed account falls
        back to the retail rule and is stopped, rather than slipping through by being
        labelled wholesale.
        """
        self.ensure_one()
        partner = self.partner_id
        return bool(
            self.is_wholesale
            and partner.is_approved_wholesale
            and partner.wholesale_licence_state in ('valid', 'expiring')
        )

    def action_confirm(self):
        guard = self.env['pharmacy.prescription.guard']
        for order in self:
            # Orders created BY a dispensing are already covered: the pharmacist
            # verified the prescription and the dispensing ran the batch checks.
            if self.env.context.get('pharmacy_dispensing'):
                continue
            # WHOLESALE is a different legal transaction (PRD 111). A wholesaler
            # supplying a licensed pharmacy or hospital is not dispensing to a patient,
            # and there is no prescription to attach - the buyer's trading licence is
            # what authorises the sale, and that is checked separately by the wholesale
            # rules in pharmacy_wholesale.py. Demanding a prescription here made bulk
            # distribution impossible: the retail guard blocked every wholesale order
            # for prescription medicine.
            if order._is_licensed_wholesale_order():
                continue
            products = order.order_line.mapped('product_id')
            guard._check_products_sellable(
                products, order.pharmacy_prescription_id,
                _('Order %s', order.name),
                wholesale_partner=order.partner_id if order.is_wholesale else None)
        return super().action_confirm()


class PosOrder(models.Model):
    _name = 'pos.order'
    _inherit = ['pos.order']

    pharmacy_prescription_id = fields.Many2one(
        'pharmacy.prescription', string='Prescription', copy=False, index=True)

    @api.model_create_multi
    def create(self, vals_list):
        """Check pharmacy rules on POS orders that actually contain pharmacy products.

        NOT gated on pos.config.is_pharmacy, deliberately. Whether a prescription-only
        medicine may be sold is a legal question about the PRODUCT; if unticking a
        checkbox on the till switched the rule off, the checkbox would be the bypass.

        It is gated on the order CONTENTS instead, so a restaurant or hardware till -
        which never sells a medicine - exits on the first line below and pays nothing
        for a module its industry does not use.
        """
        orders = super().create(vals_list)
        guard = self.env['pharmacy.prescription.guard']
        for order in orders:
            products = order.lines.mapped('product_id')
            if not any(products.mapped('is_medicine')):
                continue
            guard._check_products_sellable(
                products, order.pharmacy_prescription_id,
                _('Point of sale order %s', order.name or ''))
            order._log_pos_controlled_movements()
        return orders

    def _log_pos_controlled_movements(self):
        """Write any controlled substance sold at the till to the register.

        A controlled drug leaving over the counter is a regulated movement just like a
        backend dispensing, so it cannot escape the register simply because it went
        through POS. The batch comes from the POS lot capture; if the cashier sold it
        without a lot (mis-configuration) we still log the movement, lot blank, so the
        physical count can be reconciled.
        """
        self.ensure_one()
        Register = self.env['pharmacy.controlled.log']
        for line in self.lines.filtered(lambda l: l.product_id.is_controlled):
            lot = line.pack_lot_ids[:1].lot_name or ''
            lot_rec = self.env['stock.lot'].sudo().search([
                ('name', '=', lot), ('product_id', '=', line.product_id.id),
            ], limit=1) if lot else self.env['stock.lot']
            Register.sudo().create({
                'product_id': line.product_id.id,
                'lot_id': lot_rec.id or False,
                'movement_type': 'dispense',
                'quantity': -abs(line.qty),
                'date': self.date_order or fields.Datetime.now(),
                'pharmacist_id': self.user_id.id or self.env.user.id,
                'patient_id': self.partner_id.id or False,
                'prescription_id': self.pharmacy_prescription_id.id or False,
                'reason': _('Point of sale: %s', self.name or ''),
                'company_id': self.company_id.id,
            })


class StockPicking(models.Model):
    _name = 'stock.picking'
    _inherit = ['stock.picking']

    has_cold_chain = fields.Boolean(
        compute='_compute_has_cold_chain', store=True,
        help='This delivery contains medicine that must stay refrigerated.')

    @api.depends('move_ids.product_id.requires_cold_storage')
    def _compute_has_cold_chain(self):
        for picking in self:
            picking.has_cold_chain = any(
                picking.move_ids.mapped('product_id.requires_cold_storage'))
