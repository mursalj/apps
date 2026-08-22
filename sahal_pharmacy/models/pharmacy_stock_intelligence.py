# -*- coding: utf-8 -*-
"""Stock intelligence: what is about to run out, and what is never going to sell.

PRD 60-63 asks four questions the module could not answer:

    What is out of stock?          -> a patient turned away
    What is running low?           -> a patient about to be turned away
    What has not moved in months?  -> cash sitting on a shelf, heading for expiry
    What did expiry cost us?       -> the number that makes the other three matter

None of it needs new tables. Odoo already holds the stock (stock.quant), the movements
(stock.move) and the reorder rules (stock.warehouse.orderpoint); what it lacks is the
pharmacy's reading of them. These are queries with a menu in front of them, plus one
internal digest so nobody has to remember to look.

The digest is INTERNAL - it notifies pharmacy managers inside Odoo. Nothing here emails a
customer: on this platform customer-contacting crons ship disabled and are switched on
deliberately (docs/PROD_PENDING_CHANGES.md), and a stock alert is nobody's business but
the pharmacy's.
"""

import logging

from odoo import models, fields, api, _

_logger = logging.getLogger(__name__)

DEFAULT_DEAD_STOCK_DAYS = 120


class PharmacyStockIntelligence(models.AbstractModel):
    _name = 'pharmacy.stock.intelligence'
    _description = 'Pharmacy Stock Intelligence'

    @api.model
    def _dead_stock_days(self):
        param = self.env['ir.config_parameter'].sudo().get_param(
            'sahal_pharmacy.dead_stock_days', DEFAULT_DEAD_STOCK_DAYS)
        try:
            return max(int(param), 1)
        except (TypeError, ValueError):
            return DEFAULT_DEAD_STOCK_DAYS

    @api.model
    def _medicine_domain(self):
        return [('is_medicine', '=', True), ('is_storable', '=', True)]

    @api.model
    def _stock_positions(self):
        """{product_id: qty on hand} for every medicine, in one query."""
        groups = self.env['stock.quant'].sudo()._read_group(
            [('product_id.is_medicine', '=', True),
             ('location_id.usage', '=', 'internal')],
            ['product_id'], ['quantity:sum'])
        return {product.id: qty for product, qty in groups}

    @api.model
    def _out_of_stock(self):
        """Medicines a customer would be turned away for today."""
        positions = self._stock_positions()
        products = self.env['product.product'].search(self._medicine_domain())
        return products.filtered(lambda p: positions.get(p.id, 0.0) <= 0)

    @api.model
    def _low_stock(self):
        """Medicines at or below their reorder point, but not yet out.

        The reorder point is the pharmacy's own judgement of "low" and already exists in
        Odoo, so it is used rather than a threshold invented here. Products with no
        reorder rule cannot be low - there is nothing to be low against - which is worth
        knowing in itself.
        """
        positions = self._stock_positions()
        orderpoints = self.env['stock.warehouse.orderpoint'].sudo().search([
            ('product_id.is_medicine', '=', True)])
        low = self.env['product.product']
        for orderpoint in orderpoints:
            on_hand = positions.get(orderpoint.product_id.id, 0.0)
            if 0 < on_hand <= orderpoint.product_min_qty:
                low |= orderpoint.product_id
        return low

    @api.model
    def _dead_stock(self, days=None):
        """Medicines holding stock that nothing has taken out in `days`.

        Measured on outgoing stock moves rather than on sales lines, so a transfer to
        another branch counts as movement - the stock did leave, which is the question.
        """
        days = days or self._dead_stock_days()
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        positions = self._stock_positions()
        holding = [pid for pid, qty in positions.items() if qty > 0]
        if not holding:
            return self.env['product.product']
        moved = self.env['stock.move'].sudo()._read_group(
            [('product_id', 'in', holding), ('state', '=', 'done'),
             ('date', '>=', cutoff),
             ('location_dest_id.usage', 'in', ('customer', 'production', 'inventory')),
             ],
            ['product_id'], ['__count'])
        moved_ids = {product.id for product, _count in moved}
        return self.env['product.product'].browse(
            [pid for pid in holding if pid not in moved_ids])

    @api.model
    def _expiry_loss(self, date_from=None, date_to=None):
        """Value of stock that expired on hand in the window.

        Cost, not retail: expired stock was never sold, so the loss is what the pharmacy
        paid for it.
        """
        domain = [
            ('product_id.is_medicine', '=', True),
            ('location_id.usage', '=', 'internal'),
            ('quantity', '>', 0),
            ('lot_id.expiration_date', '!=', False),
            ('lot_id.expiration_date', '<', fields.Datetime.now()),
        ]
        if date_from:
            domain.append(('lot_id.expiration_date', '>=',
                           fields.Datetime.to_datetime(date_from)))
        if date_to:
            domain.append(('lot_id.expiration_date', '<=',
                           fields.Datetime.to_datetime(date_to)))
        quants = self.env['stock.quant'].sudo().search(domain)
        return {
            'quantity': sum(quants.mapped('quantity')),
            'value': sum(q.quantity * (q.product_id.standard_price or 0.0)
                         for q in quants),
            'lines': len(quants),
            'quant_ids': quants.ids,
        }

    @api.model
    def _cron_stock_alert(self):
        """Daily internal digest for pharmacy managers. Never contacts a customer."""
        out = self._out_of_stock()
        low = self._low_stock()
        dead = self._dead_stock()
        loss = self._expiry_loss()
        if not (out or low or dead or loss['value']):
            _logger.info("Pharmacy stock scan: nothing to report.")
            return 0

        body = _(
            "<strong>Pharmacy stock</strong><br/>"
            "Out of stock: %(out)s<br/>"
            "Low stock: %(low)s<br/>"
            "No movement in %(days)s days: %(dead)s<br/>"
            "Expired stock on hand: %(lines)s batch(es), value %(value).2f",
            out=len(out), low=len(low), days=self._dead_stock_days(),
            dead=len(dead), lines=loss['lines'], value=loss['value'])

        # The digest is attached to the PHARMACY, not to each user: res.users is not a
        # mail thread (activities on it raise), and "this branch needs attention" is
        # where a manager expects to find it anyway.
        group = self.env.ref('sahal_pharmacy.group_pharmacy_manager',
                             raise_if_not_found=False)
        managers = group.all_user_ids if group else self.env['res.users']
        profiles = self.env['pharmacy.profile'].sudo().search(
            [('company_id', '=', self.env.company.id)])
        if not profiles or not managers:
            # Nothing to hang it on, or nobody to tell. The log still carries the
            # numbers rather than the scan passing in silence.
            _logger.warning(
                "Pharmacy stock digest not delivered (%s pharmacy profile(s), %s "
                "manager(s)): %s", len(profiles), len(managers),
                body.replace('<br/>', ' | '))
            return len(out) + len(low) + len(dead)

        todo = self.env.ref('mail.mail_activity_data_todo')
        for profile in profiles:
            profile.message_post(body=body)
            for user in managers:
                self.env['mail.activity'].sudo().create({
                    'res_model_id': self.env['ir.model']._get_id('pharmacy.profile'),
                    'res_id': profile.id,
                    'activity_type_id': todo.id,
                    'summary': _('Pharmacy stock needs attention'),
                    'note': body,
                    'user_id': user.id,
                    'date_deadline': fields.Date.today(),
                })
        _logger.info(
            "Pharmacy stock scan: %s out, %s low, %s dead, expired value %.2f",
            len(out), len(low), len(dead), loss['value'])
        return len(out) + len(low) + len(dead)

    # ------------------------------------------------------------------
    # Menu actions
    # ------------------------------------------------------------------
    @api.model
    def action_out_of_stock(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Out of Stock'),
            'res_model': 'product.product',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self._out_of_stock().ids)],
        }

    @api.model
    def action_low_stock(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Low Stock'),
            'res_model': 'product.product',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self._low_stock().ids)],
        }

    @api.model
    def action_dead_stock(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('No Movement in %s Days', self._dead_stock_days()),
            'res_model': 'product.product',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self._dead_stock().ids)],
        }

    @api.model
    def action_expiry_loss(self):
        loss = self._expiry_loss()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Expired Stock on Hand'),
            'res_model': 'stock.quant',
            'view_mode': 'list,pivot',
            'domain': [('id', 'in', loss['quant_ids'])],
        }
