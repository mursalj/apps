# -*- coding: utf-8 -*-
"""Expiry alerting (BRD 5.8 and BRD 7 notifications).

FEFO picking and the expiry dates themselves come from product_expiry; what it does
not do is tell anyone. This adds the alerting: a daily scan that reports batches
expiring inside a configurable window, and blocks expired stock from being sold.

The threshold lives in a system parameter (sahal_pharmacy.near_expiry_days) so a
pharmacy can tune it without a code change.
"""

import logging

from odoo import models, fields, api, _

_logger = logging.getLogger(__name__)

DEFAULT_NEAR_EXPIRY_DAYS = 90


class StockQuantPharmacy(models.Model):
    _inherit = 'stock.quant'

    lot_expiration_date = fields.Datetime(
        related='lot_id.expiration_date', store=True, readonly=True,
        string='Batch Expiry')
    lot_is_expired = fields.Boolean(related='lot_id.is_expired', readonly=True)


class PharmacyExpiryAlert(models.AbstractModel):
    """Scheduled expiry surveillance. Abstract: it owns behaviour, not data."""
    _name = 'pharmacy.expiry.alert'
    _description = 'Pharmacy Expiry Alerting'

    @api.model
    def _near_expiry_days(self):
        param = self.env['ir.config_parameter'].sudo().get_param(
            'sahal_pharmacy.near_expiry_days', DEFAULT_NEAR_EXPIRY_DAYS)
        try:
            return max(int(param), 1)
        except (TypeError, ValueError):
            _logger.warning(
                "Invalid sahal_pharmacy.near_expiry_days=%r; using %s",
                param, DEFAULT_NEAR_EXPIRY_DAYS)
            return DEFAULT_NEAR_EXPIRY_DAYS

    @api.model
    def _expiring_quants(self, days=None):
        """Quants of medicines with stock on hand expiring within the window."""
        days = days or self._near_expiry_days()
        horizon = fields.Datetime.add(fields.Datetime.now(), days=days)
        return self.env['stock.quant'].sudo().search([
            ('product_id.is_medicine', '=', True),
            ('quantity', '>', 0),
            ('lot_id.expiration_date', '!=', False),
            ('lot_id.expiration_date', '<=', horizon),
            ('location_id.usage', '=', 'internal'),
        ], order='lot_expiration_date')

    @api.model
    def _cron_expiry_alert(self):
        """Log the near-expiry and expired positions, and notify pharmacy managers.

        Deliberately a report rather than an automatic write-off: disposing of stock is
        a decision with financial and regulatory consequences, so it stays manual
        (see the Disposal workflow on the controlled drugs register).
        """
        quants = self._expiring_quants()
        if not quants:
            _logger.info("Pharmacy expiry scan: nothing expiring soon.")
            return 0

        now = fields.Datetime.now()
        expired = quants.filtered(lambda q: q.lot_expiration_date < now)
        upcoming = quants - expired

        _logger.info(
            "Pharmacy expiry scan: %d expired position(s), %d expiring within %d days.",
            len(expired), len(upcoming), self._near_expiry_days())

        # Notify via activities on the products so the alert lands in someone's inbox
        # rather than only in a log file.
        manager_group = self.env.ref(
            'sahal_pharmacy.group_pharmacy_manager', raise_if_not_found=False)
        if not manager_group or not manager_group.users:
            return len(quants)

        for quant in expired[:50]:  # cap: an alert flood is an ignored alert
            quant.product_id.activity_schedule(
                'mail.mail_activity_data_todo',
                summary=_('Expired stock: batch %s', quant.lot_id.name),
                note=_('%(qty)s %(uom)s of %(product)s in batch %(lot)s expired on '
                       '%(date)s and must be quarantined for disposal.',
                       qty=quant.quantity, uom=quant.product_uom_id.name,
                       product=quant.product_id.display_name,
                       lot=quant.lot_id.name,
                       date=fields.Date.to_date(quant.lot_expiration_date)),
                user_id=manager_group.users[0].id,
            )
        return len(quants)

    @api.model
    def action_open_near_expiry(self):
        """Open the near-expiry stock list (BRD 6: Near Expiry report)."""
        days = self._near_expiry_days()
        horizon = fields.Datetime.add(fields.Datetime.now(), days=days)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Near-Expiry Stock (next %s days)', days),
            'res_model': 'stock.quant',
            'view_mode': 'list,form',
            'domain': [
                ('product_id.is_medicine', '=', True),
                ('quantity', '>', 0),
                ('lot_id.expiration_date', '!=', False),
                ('lot_id.expiration_date', '<=', horizon),
                ('location_id.usage', '=', 'internal'),
            ],
            'context': {'search_default_group_by_product': 1},
        }

    @api.model
    def action_open_expired(self):
        """Open expired stock still on hand (BRD 6: Expired Medicines report)."""
        return {
            'type': 'ir.actions.act_window',
            'name': _('Expired Stock on Hand'),
            'res_model': 'stock.quant',
            'view_mode': 'list,form',
            'domain': [
                ('product_id.is_medicine', '=', True),
                ('quantity', '>', 0),
                ('lot_id.expiration_date', '!=', False),
                ('lot_id.expiration_date', '<', fields.Datetime.now()),
                ('location_id.usage', '=', 'internal'),
            ],
        }
