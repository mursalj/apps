# -*- coding: utf-8 -*-
"""Utility stamps on the customer invoice.

A utility bill is not a generic sales invoice: it belongs to a service account and to a
billing period, and the customer expects to see the readings it was derived from. Storing
those on account.move keeps the link queryable (the account's ledger, the billing run's
output, the statement) instead of hidden inside line descriptions.
"""

from odoo import models, fields


class AccountMove(models.Model):
    _inherit = 'account.move'

    utility_account_id = fields.Many2one(
        'utility.service.account', string='Service Account', index=True, copy=False,
        help='Utility service account this bill was raised for.')
    utility_period_start = fields.Date('Service Period From', copy=False)
    utility_period_end = fields.Date('Service Period To', copy=False)
    utility_reading_ids = fields.One2many('utility.meter.reading', 'invoice_id',
                                          string='Billed Readings')
    utility_billing_run_id = fields.Many2one(
        'utility.billing.run', string='Billing Run', index=True, copy=False,
        help='The approved run this bill came out of - the audit trail from invoice '
             'back to the readings and the person who approved them.')
    utility_is_estimated = fields.Boolean(
        'Estimated Bill', copy=False,
        help='At least one reading behind this bill was an estimate rather than an '
             'actual reading. Shown on the printed bill (SRS 14).')
