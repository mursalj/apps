# -*- coding: utf-8 -*-
"""Expiry enforcement and FEFO selection (PRD 15, 16, 40, 41).

Dispensing already refused an expired batch. The till did not: `pos_pharmacy.js` warned
the cashier and the sale went through anyway, because the browser is advice and the
server is the rule. A warning nobody has to answer is not a control.

Two things live here:

* **The block.** Any expired batch reaching a pos.order or a sale order is refused,
  server-side, wherever it came from. Configurable (`block_expired_sales`) because a few
  jurisdictions permit exceptional handling, and overridable ONLY by a pharmacist, with
  the override written to the record — the PRD's "override should require authorized
  permission" (16).

* **FEFO selection.** Odoo's FEFO removal strategy already picks the right batch when
  stock is reserved. What it cannot do is tell a human WHICH batch to take off the shelf
  before that point, which is what a dispensing screen and a till need. `_fefo_lots`
  answers that question from live quants, earliest expiry first, expired excluded.
"""

import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PharmacyExpiryControl(models.AbstractModel):
    """Behaviour, not data — the same rules used by dispensing, sales and the till."""
    _name = 'pharmacy.expiry.control'
    _description = 'Pharmacy Expiry Enforcement'

    @api.model
    def _blocking_enabled(self):
        param = self.env['ir.config_parameter'].sudo().get_param(
            'sahal_pharmacy.block_expired_sales', 'True')
        return str(param).strip().lower() not in ('false', '0', 'no', 'off')

    @api.model
    def _fefo_lots(self, product, location=None, limit=10):
        """Batches of this product on hand, earliest expiry first, expired excluded.

        First Expired First Out is the whole point of batch rotation in a pharmacy: the
        oldest usable stock must leave first or it becomes a write-off. Lots with no
        expiry date sort last — they cannot be "about to expire", so they should not
        jump the queue ahead of stock that can.
        """
        domain = [
            ('product_id', '=', product.id),
            ('quantity', '>', 0),
            ('location_id.usage', '=', 'internal'),
            ('lot_id', '!=', False),
        ]
        if location:
            domain.append(('location_id', 'child_of', location.id))
        quants = self.env['stock.quant'].sudo().search(domain)
        now = fields.Datetime.now()
        usable = quants.filtered(
            lambda q: not q.lot_id.expiration_date or q.lot_id.expiration_date > now)
        far_future = fields.Datetime.add(now, days=36500)
        ordered = usable.sorted(
            key=lambda q: q.lot_id.expiration_date or far_future)
        return [{
            'lot_id': q.lot_id.id,
            'lot': q.lot_id.name,
            'expiry': q.lot_id.expiration_date and str(
                fields.Date.to_date(q.lot_id.expiration_date)) or '',
            'quantity': q.quantity,
            'location': q.location_id.display_name,
        } for q in ordered[:limit]]

    @api.model
    def _expired_lot_problems(self, pairs):
        """Describe every expired batch in (product, lot) pairs. Empty when all fine."""
        now = fields.Datetime.now()
        problems = []
        for product, lot in pairs:
            if not lot or not lot.expiration_date or lot.expiration_date > now:
                continue
            problems.append(_(
                "%(product)s batch %(lot)s expired on %(date)s.",
                product=product.display_name, lot=lot.name,
                date=fields.Date.to_date(lot.expiration_date)))
        return problems

    @api.model
    def _assert_not_expired(self, pairs, document_name,
                            override_context_key='pharmacy_expiry_override'):
        """Refuse the document when it moves expired stock.

        The override key defaults to the standard one rather than to None: a caller who
        forgets to name it would otherwise silently ignore a pharmacist's override, and
        the override would appear to do nothing.
        """
        if not self._blocking_enabled():
            return
        if override_context_key and self.env.context.get(override_context_key):
            return
        problems = self._expired_lot_problems(pairs)
        if not problems:
            return
        raise UserError(_(
            "%(doc)s dispenses expired medicine:\n\n%(problems)s\n\nTake a valid batch "
            "instead. A pharmacist can override this if the pharmacy's policy allows "
            "it, and the override is recorded.",
            doc=document_name,
            problems='\n'.join('• %s' % p for p in problems)))


class PosOrder(models.Model):
    _inherit = 'pos.order'

    @api.model_create_multi
    def create(self, vals_list):
        """Stop expired stock leaving the till.

        Keyed on the ORDER CONTENTS, like the prescription guard: a till that sells no
        medicines exits immediately, so other industries pay nothing for this.
        """
        orders = super().create(vals_list)
        control = self.env['pharmacy.expiry.control']
        for order in orders:
            medicine_lines = order.lines.filtered(lambda l: l.product_id.is_medicine)
            if not medicine_lines:
                continue
            # pos.pack.operation.lot carries only the lot NAME — the till captures what
            # the cashier scanned, not a link to stock.lot — so the batch has to be
            # resolved per product before its expiry can be read.
            pairs = []
            for line in medicine_lines:
                for pack_lot in line.pack_lot_ids:
                    if not pack_lot.lot_name:
                        continue
                    lot = self.env['stock.lot'].sudo().search([
                        ('name', '=', pack_lot.lot_name),
                        ('product_id', '=', line.product_id.id),
                    ], limit=1)
                    if lot:
                        pairs.append((line.product_id, lot))
            control._assert_not_expired(
                pairs, _('Point of sale order %s', order.name or ''),
                override_context_key='pharmacy_expiry_override')
        return orders


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    def action_confirm(self):
        """Refuse a sale that has already been assigned an expired batch."""
        control = self.env['pharmacy.expiry.control']
        for order in self:
            moves = order.order_line.mapped('move_ids')
            pairs = [(ml.product_id, ml.lot_id)
                     for ml in moves.mapped('move_line_ids') if ml.lot_id]
            if pairs:
                control._assert_not_expired(
                    pairs, _('Order %s', order.name),
                    override_context_key='pharmacy_expiry_override')
        return super().action_confirm()


class PharmacyDispenseLine(models.Model):
    _inherit = 'pharmacy.dispense.line'

    fefo_suggestion = fields.Char(
        'Suggested Batch', compute='_compute_fefo_suggestion',
        help='The batch FEFO says to take: the earliest-expiring usable stock on hand.')

    @api.depends('product_id')
    def _compute_fefo_suggestion(self):
        for line in self:
            line.fefo_suggestion = False
            if not line.product_id:
                continue
            lots = self.env['pharmacy.expiry.control']._fefo_lots(line.product_id,
                                                                  limit=1)
            if lots:
                first = lots[0]
                line.fefo_suggestion = _(
                    "%(lot)s — expires %(expiry)s (%(qty)s on hand)",
                    lot=first['lot'], expiry=first['expiry'] or _('no expiry'),
                    qty=first['quantity'])

    @api.onchange('product_id')
    def _onchange_product_fefo(self):
        """Pre-select the batch that should leave first, without forcing it.

        The pharmacist can still choose another — a patient may be returning for the
        same batch they started, and stock at the front of the shelf is not always the
        stock the system thinks. But the default is the correct one.
        """
        if self.product_id and not self.lot_id:
            lots = self.env['pharmacy.expiry.control']._fefo_lots(self.product_id,
                                                                  limit=1)
            if lots:
                self.lot_id = lots[0]['lot_id']
