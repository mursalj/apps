# -*- coding: utf-8 -*-
"""Wholesale pharmaceutical distribution (PRD 46-51, 111).

A wholesaler sells to pharmacies, clinics, hospitals, NGOs and government agencies —
licensed businesses, on credit, in cartons, at prices that depend on who is buying and
how much. The PRD is explicit that this must NOT run through the retail till.

It doesn't need to: Odoo's quotation -> sales order -> delivery -> invoice chain is
already the wholesale flow, pricelists already express customer and volume pricing, and
`res.partner.credit_limit` already exists. What Odoo has no concept of:

* whether the buyer is **licensed** to buy medicines at all, and whether that licence
  is still valid on the day of the order;
* a **credit hold** — a deliberate stop that survives until someone with authority
  lifts it, as opposed to a limit that merely warns;
* **minimum order quantities** in an industry that sells by the carton.

Those three are what this module adds. Everything else is configuration of Odoo.
"""

import logging

from dateutil.relativedelta import relativedelta

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

WHOLESALE_CATEGORIES = [
    ('pharmacy', 'Retail Pharmacy'),
    ('clinic', 'Clinic'),
    ('hospital', 'Hospital'),
    ('ngo', 'NGO'),
    ('government', 'Government Agency'),
    ('distributor', 'Distributor'),
    ('other', 'Other'),
]


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # --- wholesale customer profile (PRD 47) --------------------------------
    is_wholesale_customer = fields.Boolean(
        'Wholesale Customer',
        help='Buys in bulk on a business account rather than over the counter.')
    wholesale_category = fields.Selection(WHOLESALE_CATEGORIES, string='Customer Type')
    wholesale_licence_no = fields.Char('Trading Licence No.')
    wholesale_licence_expiry = fields.Date('Licence Expires')
    wholesale_licence_state = fields.Selection([
        ('none', 'Not Recorded'),
        ('valid', 'Valid'),
        ('expiring', 'Expiring Soon'),
        ('expired', 'Expired'),
    ], string='Licence Status', compute='_compute_wholesale_licence_state', store=True)
    is_approved_wholesale = fields.Boolean(
        'Approved to Buy Medicines', tracking=True,
        help='A licensed business the pharmacy has agreed to supply. Unapproved '
             'accounts are warned about or blocked on order confirmation.')

    # --- credit (PRD 51) ----------------------------------------------------
    # credit_limit and the receivable balance are Odoo's; only the HOLD is ours.
    credit_hold = fields.Boolean(
        'On Credit Hold', tracking=True,
        help='Stops new orders regardless of the limit. Set deliberately, and only '
             'cleared by a pharmacy manager.')
    credit_hold_reason = fields.Char('Credit Hold Reason', tracking=True)
    credit_available = fields.Monetary(
        'Available Credit', compute='_compute_credit_available',
        currency_field='currency_id')
    credit_used_pct = fields.Float('Credit Used (%)', compute='_compute_credit_available')

    @api.depends('wholesale_licence_expiry')
    def _compute_wholesale_licence_state(self):
        today = fields.Date.today()
        horizon = today + relativedelta(days=self._licence_alert_days())
        for partner in self:
            expiry = partner.wholesale_licence_expiry
            if not expiry:
                partner.wholesale_licence_state = 'none'
            elif expiry < today:
                partner.wholesale_licence_state = 'expired'
            elif expiry <= horizon:
                partner.wholesale_licence_state = 'expiring'
            else:
                partner.wholesale_licence_state = 'valid'

    @api.depends('credit_limit', 'credit', 'credit_hold')
    def _compute_credit_available(self):
        for partner in self:
            limit = partner.credit_limit or 0.0
            used = partner.credit or 0.0
            partner.credit_available = max(limit - used, 0.0) if limit else 0.0
            partner.credit_used_pct = (used / limit * 100) if limit else 0.0

    def action_place_on_credit_hold(self):
        for partner in self:
            partner.write({'credit_hold': True})
            partner.message_post(body=_(
                "Placed on credit hold. %s", partner.credit_hold_reason or ''))

    def action_release_credit_hold(self):
        """Only a manager lifts a hold — that is the whole point of having one."""
        if not self.env.user.has_group('sahal_pharmacy.group_pharmacy_manager'):
            raise UserError(_(
                "Releasing a credit hold is a Pharmacy Manager decision."))
        for partner in self:
            partner.write({'credit_hold': False, 'credit_hold_reason': False})
            partner.message_post(body=_("Credit hold released."))


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    # --- wholesale order quantities (PRD 50) --------------------------------
    wholesale_min_qty = fields.Float(
        'Minimum Order Qty', help='Smallest quantity a wholesale customer may order. '
                                  'Zero means no minimum.')
    wholesale_max_qty = fields.Float(
        'Maximum Order Qty', help='Largest quantity per order, for allocation of '
                                  'scarce stock. Zero means no maximum.')
    wholesale_multiple_qty = fields.Float(
        'Order Multiple',
        help='Sold in whole cases: an order must be a multiple of this. e.g. 20 when '
             'a carton is 20 boxes.')

    @api.constrains('wholesale_min_qty', 'wholesale_max_qty')
    def _check_wholesale_quantities(self):
        for product in self:
            if product.wholesale_max_qty and product.wholesale_min_qty and \
                    product.wholesale_max_qty < product.wholesale_min_qty:
                raise ValidationError(_(
                    "'%s': the maximum order quantity is below the minimum.",
                    product.display_name))


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    is_wholesale = fields.Boolean(
        'Wholesale Order', compute='_compute_is_wholesale', store=True, readonly=False,
        help='Set from the customer, and can be overridden for a one-off bulk sale.')
    wholesale_block_reason = fields.Text(
        'Wholesale Check', compute='_compute_wholesale_block_reason',
        help='Why this order cannot be confirmed as it stands.')

    @api.depends('partner_id')
    def _compute_is_wholesale(self):
        for order in self:
            order.is_wholesale = order.partner_id.is_wholesale_customer

    @api.depends('partner_id', 'order_line.product_uom_qty', 'order_line.product_id',
                 'amount_total', 'is_wholesale')
    def _compute_wholesale_block_reason(self):
        for order in self:
            order.wholesale_block_reason = '\n'.join(order._wholesale_problems()) or False

    def _wholesale_problems(self, licence_only=False):
        """Everything standing between this order and a confirmed commitment.

        `licence_only` returns just the problems a manager may NOT override: whether the
        buyer is licensed and approved to receive prescription medicine at all. Credit
        limits and order quantities are commercial judgements the pharmacy can take on
        its own; supplying prescription medicine to an unlicensed business is not.
        """
        self.ensure_one()
        if not self.is_wholesale or not self.partner_id:
            return []
        partner = self.partner_id
        problems = []

        # --- licence -------------------------------------------------------
        if self.order_line.filtered(lambda l: l.product_id.is_medicine):
            if not partner.is_approved_wholesale:
                problems.append(_(
                    "%s is not approved to buy medicines wholesale.",
                    partner.display_name))
            if partner.wholesale_licence_state == 'expired':
                problems.append(_(
                    "%(name)s's trading licence expired on %(date)s.",
                    name=partner.display_name,
                    date=partner.wholesale_licence_expiry))
            elif partner.wholesale_licence_state == 'none' and \
                    partner.is_approved_wholesale:
                problems.append(_(
                    "No trading licence is recorded for %s.", partner.display_name))

        if licence_only:
            return problems

        # --- credit --------------------------------------------------------
        if partner.credit_hold:
            problems.append(_(
                "%(name)s is on credit hold. %(reason)s",
                name=partner.display_name, reason=partner.credit_hold_reason or ''))
        elif partner.credit_limit:
            projected = (partner.credit or 0.0) + self.amount_total
            if projected > partner.credit_limit:
                problems.append(_(
                    "This order takes %(name)s to %(projected)s against a credit limit "
                    "of %(limit)s (currently owing %(owing)s).",
                    name=partner.display_name, projected=projected,
                    limit=partner.credit_limit, owing=partner.credit or 0.0))

        # --- order quantities ----------------------------------------------
        for line in self.order_line:
            product = line.product_id.product_tmpl_id
            qty = line.product_uom_qty
            if product.wholesale_min_qty and qty < product.wholesale_min_qty:
                problems.append(_(
                    "%(product)s has a minimum order of %(min)s; this line orders "
                    "%(qty)s.", product=line.product_id.display_name,
                    min=product.wholesale_min_qty, qty=qty))
            if product.wholesale_max_qty and qty > product.wholesale_max_qty:
                problems.append(_(
                    "%(product)s is limited to %(max)s per order; this line orders "
                    "%(qty)s.", product=line.product_id.display_name,
                    max=product.wholesale_max_qty, qty=qty))
            multiple = product.wholesale_multiple_qty
            if multiple and qty % multiple:
                problems.append(_(
                    "%(product)s is sold in multiples of %(mult)s; %(qty)s is not.",
                    product=line.product_id.display_name, mult=multiple, qty=qty))
        return problems

    @api.model
    def _wholesale_enforcement(self):
        return self.env['ir.config_parameter'].sudo().get_param(
            'sahal_pharmacy.wholesale_enforcement', 'block')

    def action_confirm(self):
        """Licence, credit and order quantities are checked before a commitment.

        Blocking by default, unlike the supplier check: confirming a wholesale order
        promises stock to a customer and creates a receivable, and both are hard to
        walk back. A manager can override — with the reason recorded on the order.
        """
        enforcement = self._wholesale_enforcement()
        if enforcement != 'off':
            for order in self:
                problems = order._wholesale_problems()
                if not problems:
                    continue
                if enforcement == 'block' and not self.env.context.get(
                        'pharmacy_wholesale_override'):
                    raise UserError(_(
                        "This wholesale order cannot be confirmed:\n\n%(problems)s\n\n"
                        "A Pharmacy Manager can override this from the order.",
                        problems='\n'.join('• %s' % p for p in problems)))
                order.message_post(body=_(
                    "<strong>Wholesale check</strong><br/>%s",
                    '<br/>'.join('• %s' % p for p in problems)))
        return super().action_confirm()

    def action_confirm_wholesale_override(self):
        """Confirm despite the warnings, on a manager's authority and on the record.

        The override covers the pharmacy's own commercial rules — credit limit, order
        quantities, credit hold. It does NOT cover the buyer's licence: supplying
        prescription medicine to a business that is not licensed and approved to receive
        it is not a decision a manager gets to make, and the retail prescription guard
        would refuse the order anyway. Saying so here is better than a button that looks
        like it worked and then throws.
        """
        self.ensure_one()
        if not self.env.user.has_group('sahal_pharmacy.group_pharmacy_manager'):
            raise UserError(_(
                "Overriding a wholesale block is a Pharmacy Manager decision."))
        blocking = self._wholesale_problems(licence_only=True)
        if blocking:
            raise UserError(_(
                "This cannot be overridden:\n\n%(problems)s\n\nThe buyer's licence is "
                "a legal requirement, not a pharmacy policy. Approve the account and "
                "record a current trading licence, then confirm normally.",
                problems='\n'.join('• %s' % p for p in blocking)))
        problems = self._wholesale_problems()
        self.message_post(body=_(
            "<strong>Wholesale block overridden by %(user)s</strong><br/>%(problems)s",
            user=self.env.user.display_name,
            problems='<br/>'.join('• %s' % p for p in problems)))
        # Also to the central override log, so "show me every override last month" is
        # one list rather than a search across five models' chatter.
        self.env['pharmacy.override'].log(
            'wholesale', '\n'.join(problems), record=self, partner=self.partner_id)
        return self.with_context(pharmacy_wholesale_override=True).action_confirm()
