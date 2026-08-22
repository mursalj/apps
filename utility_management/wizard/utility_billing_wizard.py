# -*- coding: utf-8 -*-
"""Batch invoicing wizard (BRD 4.4).

The billing specialist picks a period, optionally narrows to a utility type or specific
customers, previews how many readings match, and generates the draft invoices in one
click. It simply selects Validated, unbilled readings in the window and hands them to
utility.meter.reading._bill(), so the wizard and the cron produce identical results.
"""

from odoo import models, fields, api, _
from odoo.exceptions import UserError

from odoo.addons.utility_management.models.utility_tariff import UTILITY_TYPES


class UtilityBillingWizard(models.TransientModel):
    _name = 'utility.billing.wizard'
    _description = 'Batch Utility Billing'

    date_from = fields.Date('Readings From', required=True,
                            default=lambda s: fields.Date.today().replace(day=1))
    date_to = fields.Date('Readings To', required=True, default=fields.Date.today)
    utility_type = fields.Selection(
        UTILITY_TYPES, string='Utility Type',
        help='Leave empty to bill all utilities.')
    partner_ids = fields.Many2many(
        'res.partner', string='Customers',
        help='Leave empty to bill every customer with matching readings.')
    reading_count = fields.Integer('Matching Readings', compute='_compute_reading_count')

    def _reading_domain(self):
        self.ensure_one()
        domain = [
            ('state', '=', 'validated'),
            ('superseded', '=', False),
            ('reading_date', '>=', fields.Datetime.to_datetime(self.date_from)),
            ('reading_date', '<=', fields.Datetime.to_datetime(self.date_to).replace(
                hour=23, minute=59, second=59)),
            ('meter_id.partner_id', '!=', False),
            ('meter_id.tariff_id', '!=', False),
        ]
        if self.utility_type:
            domain.append(('utility_type', '=', self.utility_type))
        if self.partner_ids:
            domain.append(('meter_id.partner_id', 'in', self.partner_ids.ids))
        return domain

    @api.depends('date_from', 'date_to', 'utility_type', 'partner_ids')
    def _compute_reading_count(self):
        for wiz in self:
            wiz.reading_count = self.env['utility.meter.reading'].search_count(
                wiz._reading_domain()) if wiz.date_from and wiz.date_to else 0

    def action_generate(self):
        self.ensure_one()
        readings = self.env['utility.meter.reading'].search(self._reading_domain())
        if not readings:
            raise UserError(_("No Validated, unbilled readings match these criteria."))
        invoices = readings._bill()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Draft Utility Invoices'),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('id', 'in', invoices.ids)],
            'context': {'default_move_type': 'out_invoice'},
        }
