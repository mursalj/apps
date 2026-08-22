# -*- coding: utf-8 -*-
"""Reporting hooks (PRD 78-84).

The module had no graph or pivot view at all, so none of the PRD's report families
could be answered — not "how much of our revenue is prescription versus over the
counter", not "which supplier's stock keeps expiring on us", not "who is close to their
credit limit".

Almost none of that needs a new table. Odoo's own `sale.report` and `stock.quant`
already carry the numbers; what they lack is the pharmaceutical dimension to group by.
So this file adds the dimension — product type, dosage form, generic, supplier — to
models that already exist, and the views do the rest.

`stock.quant` gains the expiry dimensions for the same reason: the near-expiry list was
a cron writing to the log, which nobody can pivot.
"""

from odoo import models, fields, api


class SaleReport(models.Model):
    """Pharmaceutical dimensions on Odoo's own sales analysis."""
    _inherit = 'sale.report'

    pharma_type = fields.Selection(
        related='product_id.pharma_type', string='Product Type', readonly=True)
    is_medicine = fields.Boolean(related='product_id.is_medicine', readonly=True)
    requires_prescription = fields.Boolean(
        related='product_id.requires_prescription', string='Prescription Only',
        readonly=True)
    is_controlled = fields.Boolean(related='product_id.is_controlled', readonly=True)
    generic_id = fields.Many2one(related='product_id.generic_id', string='Generic',
                                 readonly=True)
    dosage_form_id = fields.Many2one(related='product_id.dosage_form_id',
                                     string='Dosage Form', readonly=True)
    is_wholesale_customer = fields.Boolean(
        related='partner_id.is_wholesale_customer', string='Wholesale Customer',
        readonly=True)
    wholesale_category = fields.Selection(
        related='partner_id.wholesale_category', string='Customer Type', readonly=True)


class StockQuant(models.Model):
    """Expiry as a reportable dimension, not just a nightly log line."""
    _inherit = 'stock.quant'

    pharma_type = fields.Selection(
        related='product_id.pharma_type', string='Product Type', readonly=True)
    expiry_status = fields.Selection([
        ('none', 'No Expiry'),
        ('valid', 'Valid'),
        ('near', 'Near Expiry'),
        ('expired', 'Expired'),
    ], string='Expiry Status', compute='_compute_expiry_status', search='_search_expiry_status')
    days_to_expiry = fields.Integer('Days to Expiry', compute='_compute_expiry_status')
    stock_value = fields.Monetary(
        'Stock Value', compute='_compute_stock_value', currency_field='currency_id',
        help='What this position is worth at cost — the number that makes an expiry '
             'report a financial conversation rather than a list.')
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id',
                                  readonly=True)

    @api.depends('lot_id.expiration_date')
    def _compute_expiry_status(self):
        now = fields.Datetime.now()
        horizon_days = self.env['pharmacy.expiry.alert']._near_expiry_days()
        for quant in self:
            expiry = quant.lot_id.expiration_date
            if not expiry:
                quant.expiry_status = 'none'
                quant.days_to_expiry = 0
                continue
            delta = (expiry - now).days
            quant.days_to_expiry = delta
            if delta < 0:
                quant.expiry_status = 'expired'
            elif delta <= horizon_days:
                quant.expiry_status = 'near'
            else:
                quant.expiry_status = 'valid'

    def _search_expiry_status(self, operator, value):
        """Make the status filterable without storing a field that goes stale hourly.

        Odoo 19 normalises `=` into `in` with a collection, so the value arriving here
        may be a single string OR a set of them — reading it as a plain string raises
        "unhashable type: OrderedSet" the first time anyone clicks the filter.
        """
        now = fields.Datetime.now()
        horizon = fields.Datetime.add(
            now, days=self.env['pharmacy.expiry.alert']._near_expiry_days())
        domains = {
            'none': [('lot_id.expiration_date', '=', False)],
            'expired': [('lot_id.expiration_date', '!=', False),
                        ('lot_id.expiration_date', '<', now)],
            'near': [('lot_id.expiration_date', '>=', now),
                     ('lot_id.expiration_date', '<=', horizon)],
            'valid': [('lot_id.expiration_date', '>', horizon)],
        }

        wanted = [value] if isinstance(value, str) else list(value or [])
        negate = operator in ('!=', 'not in')

        selected = [domains[v] for v in wanted if v in domains]
        if not selected:
            # Nothing recognisable to filter on: match everything rather than silently
            # returning an empty list, which would hide all stock.
            return [(1, '=', 1)]

        combined = selected[0]
        for extra in selected[1:]:
            combined = ['|'] + combined + extra
        return (['!'] + combined) if negate else combined

    @api.depends('quantity', 'product_id.standard_price')
    def _compute_stock_value(self):
        for quant in self:
            quant.stock_value = quant.quantity * (quant.product_id.standard_price or 0.0)
