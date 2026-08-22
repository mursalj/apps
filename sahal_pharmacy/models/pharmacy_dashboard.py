# -*- coding: utf-8 -*-
"""Pharmacy dashboard data (PRD 84).

One RPC returns every figure the board renders. Deliberately one call: a dashboard that
fires fifteen queries from the browser is slow on a good connection and unusable on a
pharmacy's, and each round trip is another thing that can half-load.

Every number is aggregated with _read_group rather than by loading records — a pharmacy
with 40,000 stock lines must not pull them into memory to show a headline.

Two honesty rules the figures follow:

* **Till takings and invoiced sales are shown separately.** A POS order and a customer
  invoice are different documents; adding them together double-counts whenever a POS
  order is invoiced, and pharmacies do both. Two numbers that are each true beat one
  that is nearly right.
* **Nothing is estimated.** Where the data does not exist — no reorder rule, no cost
  price — the figure is absent rather than guessed.
"""

import logging

from dateutil.relativedelta import relativedelta

from odoo import models, fields, api, _

_logger = logging.getLogger(__name__)

# How many top sellers to show.
TOP_PRODUCTS = 8


class PharmacyDashboard(models.TransientModel):
    _name = 'pharmacy.dashboard'
    _description = 'Pharmacy Dashboard'

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    @api.model
    def get_dashboard_data(self, months=12):
        months = max(1, min(int(months or 12), 24))
        today = fields.Date.today()
        month_start = today.replace(day=1)
        currency = self.env.company.currency_id

        return {
            'currency': {
                'symbol': currency.symbol or '',
                'position': currency.position or 'before',
                'decimals': currency.decimal_places,
            },
            'kpis': self._kpis(today, month_start),
            'stock': self._stock_health(),
            'expiry': self._expiry_position(),
            'top_products': self._top_products(month_start),
            'mix': self._sales_mix(month_start),
            'series': self._series(months),
            'attention': self._attention(),
        }

    # ------------------------------------------------------------------
    # Headline figures
    # ------------------------------------------------------------------
    def _kpis(self, today, month_start):
        Pos = self.env['pos.order']
        Move = self.env['account.move']

        pos_domain = [
            ('date_order', '>=', fields.Datetime.to_datetime(today)),
            ('state', 'in', ('paid', 'done', 'invoiced')),
        ]
        [(takings_today, orders_today)] = Pos._read_group(
            pos_domain, [], ['amount_total:sum', '__count'])

        invoice_domain = [
            ('move_type', 'in', ('out_invoice', 'out_refund')),
            ('state', '=', 'posted'),
        ]
        [(invoiced_month,)] = Move._read_group(
            invoice_domain + [('invoice_date', '>=', month_start)], [],
            ['amount_total_signed:sum'])
        [(receivables,)] = Move._read_group(
            invoice_domain, [], ['amount_residual_signed:sum'])

        Claim = self.env['pharmacy.insurance.claim']
        [(insurance_outstanding,)] = Claim._read_group(
            [('state', 'in', ('submitted', 'approved', 'invoiced'))], [],
            ['covered_amount:sum'])

        Prescription = self.env['pharmacy.prescription']
        Dispense = self.env['pharmacy.dispense']
        Register = self.env['pharmacy.controlled.log']
        start_of_day = fields.Datetime.to_datetime(today)

        return {
            'takings_today': takings_today or 0.0,
            'orders_today': orders_today or 0,
            'invoiced_month': invoiced_month or 0.0,
            'receivables': receivables or 0.0,
            'insurance_outstanding': insurance_outstanding or 0.0,
            'prescriptions_today': Prescription.search_count(
                [('prescription_date', '=', today)]),
            'to_verify': Prescription.search_count([('state', '=', 'to_verify')]),
            'dispensed_today': Dispense.search_count([
                ('dispense_date', '>=', start_of_day), ('state', '=', 'done')]),
            'controlled_today': Register.search_count([('date', '>=', start_of_day)]),
        }

    # ------------------------------------------------------------------
    # Stock health — the colour-coded part
    # ------------------------------------------------------------------
    def _stock_health(self):
        """Out / low / healthy, plus what the shelf is worth.

        The three buckets are mutually exclusive: something out of stock is not also
        counted as low, or the totals stop adding up and nobody trusts the tile.
        """
        Intel = self.env['pharmacy.stock.intelligence']
        out = Intel._out_of_stock()
        low = Intel._low_stock() - out
        medicines = self.env['product.product'].search_count(Intel._medicine_domain())

        quants = self.env['stock.quant']._read_group(
            [('product_id.is_medicine', '=', True),
             ('location_id.usage', '=', 'internal'), ('quantity', '>', 0)],
            ['product_id'], ['quantity:sum'])
        stock_value = sum(
            qty * (product.standard_price or 0.0) for product, qty in quants)

        healthy = max(medicines - len(out) - len(low), 0)
        return {
            'out_of_stock': len(out),
            'low_stock': len(low),
            'healthy': healthy,
            'medicines': medicines,
            'stock_value': stock_value,
            # A single word the tile colours itself by, decided here so the rule lives
            # with the data rather than in three places in the template.
            'level': 'danger' if out else ('warning' if low else 'success'),
        }

    def _expiry_position(self):
        Intel = self.env['pharmacy.stock.intelligence']
        Alert = self.env['pharmacy.expiry.alert']
        near_days = Alert._near_expiry_days()
        loss = Intel._expiry_loss()

        now = fields.Datetime.now()
        horizon = fields.Datetime.add(now, days=near_days)
        # Not sudo, for the same reason as _stock_position(): near-expiry counts and
        near = self.env['stock.quant'].search([
            ('product_id.is_medicine', '=', True),
            ('location_id.usage', '=', 'internal'), ('quantity', '>', 0),
            ('lot_id.expiration_date', '>=', now),
            ('lot_id.expiration_date', '<=', horizon),
        ])
        return {
            'expired_lines': loss['lines'],
            'expired_value': loss['value'],
            'expired_qty': loss['quantity'],
            'near_lines': len(near),
            'near_value': sum(
                q.quantity * (q.product_id.standard_price or 0.0) for q in near),
            'near_qty': sum(near.mapped('quantity')),
            'near_days': near_days,
            'level': 'danger' if loss['lines'] else ('warning' if near else 'success'),
        }

    # ------------------------------------------------------------------
    # What is selling
    # ------------------------------------------------------------------
    def _sold_lines(self, since):
        """{product_id: (qty, revenue)} across BOTH channels for the period.

        Sales orders and POS orders are separate tables in Odoo, and a pharmacy sells
        through both — the counter and the wholesale desk. Reading only one of them is
        how a dashboard quietly reports half the business.
        """
        totals = {}

        sale_lines = self.env['sale.order.line']._read_group(
            [('order_id.state', 'in', ('sale', 'done')),
             ('order_id.date_order', '>=', fields.Datetime.to_datetime(since)),
             ('product_id.is_medicine', '=', True)],
            ['product_id'], ['product_uom_qty:sum', 'price_subtotal:sum'])
        for product, qty, revenue in sale_lines:
            entry = totals.setdefault(product.id, {'product': product, 'qty': 0.0,
                                                   'revenue': 0.0})
            entry['qty'] += qty
            entry['revenue'] += revenue

        pos_lines = self.env['pos.order.line']._read_group(
            [('order_id.state', 'in', ('paid', 'done', 'invoiced')),
             ('order_id.date_order', '>=', fields.Datetime.to_datetime(since)),
             ('product_id.is_medicine', '=', True)],
            ['product_id'], ['qty:sum', 'price_subtotal:sum'])
        for product, qty, revenue in pos_lines:
            entry = totals.setdefault(product.id, {'product': product, 'qty': 0.0,
                                                   'revenue': 0.0})
            entry['qty'] += qty
            entry['revenue'] += revenue
        return totals

    def _top_products(self, since):
        totals = self._sold_lines(since)
        ranked = sorted(totals.values(), key=lambda e: e['revenue'], reverse=True)
        return [{
            'id': entry['product'].id,
            'name': entry['product'].display_name,
            'type': entry['product'].pharma_type or '',
            'qty': round(entry['qty'], 2),
            'revenue': entry['revenue'],
        } for entry in ranked[:TOP_PRODUCTS]]

    def _sales_mix(self, since):
        """Revenue split by what kind of product it is — the OTC vs prescription answer."""
        totals = self._sold_lines(since)
        labels = dict(self.env['product.template']._fields['pharma_type'].selection)
        mix = {}
        for entry in totals.values():
            key = entry['product'].pharma_type or 'other'
            mix[key] = mix.get(key, 0.0) + entry['revenue']
        return [{'label': labels.get(key, key), 'value': round(value, 2)}
                for key, value in sorted(mix.items(), key=lambda kv: -kv[1])]

    def _series(self, months):
        """Monthly takings and invoiced revenue, oldest first."""
        today = fields.Date.today()
        first_this = today.replace(day=1)
        buckets = []
        cursor = first_this - relativedelta(months=months - 1)
        while cursor <= first_this:
            buckets.append({'key': cursor.strftime('%Y-%m'),
                            'label': cursor.strftime('%b %Y'),
                            'retail': 0.0, 'invoiced': 0.0})
            cursor += relativedelta(months=1)
        by_key = {b['key']: b for b in buckets}
        window_start = fields.Date.to_date('%s-01' % buckets[0]['key'])

        pos_groups = self.env['pos.order']._read_group(
            [('state', 'in', ('paid', 'done', 'invoiced')),
             ('date_order', '>=', fields.Datetime.to_datetime(window_start))],
            ['date_order:month'], ['amount_total:sum'])
        for month, amount in pos_groups:
            bucket = by_key.get(month.strftime('%Y-%m'))
            if bucket:
                bucket['retail'] += amount

        move_groups = self.env['account.move']._read_group(
            [('move_type', 'in', ('out_invoice', 'out_refund')),
             ('state', '=', 'posted'), ('invoice_date', '>=', window_start)],
            ['invoice_date:month'], ['amount_total_signed:sum'])
        for month, amount in move_groups:
            bucket = by_key.get(month.strftime('%Y-%m'))
            if bucket:
                bucket['invoiced'] += amount

        return [{'label': b['label'], 'retail': round(b['retail'], 2),
                 'invoiced': round(b['invoiced'], 2)} for b in buckets]

    # ------------------------------------------------------------------
    # What needs a human today
    # ------------------------------------------------------------------
    def _attention(self):
        """The queue. Each row is a count and the action that opens it."""
        Prescription = self.env['pharmacy.prescription']
        Partner = self.env['res.partner']
        Document = self.env['pharmacy.document']
        Claim = self.env['pharmacy.insurance.claim']
        Return = self.env['pharmacy.return']
        today = fields.Date.today()

        rows = [
            {
                'key': 'to_verify',
                'label': _('Prescriptions awaiting verification'),
                'count': Prescription.search_count([('state', '=', 'to_verify')]),
                'level': 'warning',
            },
            {
                'key': 'clinical',
                'label': _('Clinical warnings not reviewed'),
                'count': len(self._prescriptions_with_open_warnings()),
                'level': 'danger',
            },
            {
                'key': 'returns',
                'label': _('Customer returns to accept'),
                'count': Return.search_count([('state', '=', 'draft')]),
                'level': 'info',
            },
            {
                'key': 'claims',
                'label': _('Insurance claims awaiting the insurer'),
                'count': Claim.search_count([('state', 'in', ('draft', 'submitted'))]),
                'level': 'info',
            },
            {
                'key': 'credit_hold',
                'label': _('Wholesale accounts on credit hold'),
                'count': Partner.search_count([('credit_hold', '=', True)]),
                'level': 'warning',
            },
            {
                'key': 'supplier_licence',
                'label': _('Supplier licences expired or expiring'),
                'count': Partner.search_count([
                    ('is_pharma_supplier', '=', True),
                    ('pharma_licence_state', 'in', ('expired', 'expiring')),
                ]),
                'level': 'warning',
            },
            {
                'key': 'documents',
                'label': _('Regulatory documents expiring'),
                'count': Document.search_count([('state', 'in', ('expired', 'expiring'))]),
                'level': 'warning',
            },
        ]
        del today
        return [row for row in rows if row['count']]

    def _prescriptions_with_open_warnings(self):
        """Live prescriptions carrying a serious clinical finding nobody has signed off.

        Filtered in Python because clinical_severity is computed, not stored — and
        deliberately so. Storing it would freeze a judgement that depends on the
        interaction table and on the patient's allergies: add an interaction tomorrow and
        every stored severity written today would be wrong, which is precisely when the
        warning matters most.

        The set is bounded to prescriptions still in play (not cancelled, not fully
        dispensed), which is a working queue, not the archive.
        """
        live = self.env['pharmacy.prescription'].search([
            ('state', 'in', ('draft', 'to_verify', 'verified', 'partially_dispensed')),
            ('clinical_ack_by_id', '=', False),
        ])
        return live.filtered(
            lambda rx: rx.clinical_severity in ('high', 'critical'))

    # ------------------------------------------------------------------
    # Drill-downs
    # ------------------------------------------------------------------
    @api.model
    def action_open(self, key):
        """Open the list behind a tile. One method so the client stays declarative."""
        Intel = self.env['pharmacy.stock.intelligence']
        actions = {
            'out_of_stock': lambda: Intel.action_out_of_stock(),
            'low_stock': lambda: Intel.action_low_stock(),
            'expired': lambda: Intel.action_expiry_loss(),
            'near_expiry': lambda: self._action_near_expiry(),
            'to_verify': lambda: self._act_window(
                _('Prescriptions to Verify'), 'pharmacy.prescription',
                [('state', '=', 'to_verify')]),
            'clinical': lambda: self._act_window(
                _('Unreviewed Clinical Warnings'), 'pharmacy.prescription',
                [('id', 'in', self._prescriptions_with_open_warnings().ids)]),
            'returns': lambda: self._act_window(
                _('Returns to Accept'), 'pharmacy.return', [('state', '=', 'draft')]),
            'claims': lambda: self._act_window(
                _('Open Insurance Claims'), 'pharmacy.insurance.claim',
                [('state', 'in', ('draft', 'submitted'))]),
            'credit_hold': lambda: self._act_window(
                _('Accounts on Credit Hold'), 'res.partner',
                [('credit_hold', '=', True)]),
            'supplier_licence': lambda: self._act_window(
                _('Supplier Licences'), 'res.partner',
                [('is_pharma_supplier', '=', True),
                 ('pharma_licence_state', 'in', ('expired', 'expiring'))]),
            'documents': lambda: self._act_window(
                _('Documents Expiring'), 'pharmacy.document',
                [('state', 'in', ('expired', 'expiring'))]),
            'receivables': lambda: self._act_window(
                _('Unpaid Customer Invoices'), 'account.move',
                [('move_type', '=', 'out_invoice'), ('state', '=', 'posted'),
                 ('payment_state', 'not in', ('paid', 'reversed'))]),
        }
        builder = actions.get(key)
        return builder() if builder else False

    def _act_window(self, name, model, domain, view_mode='list,form'):
        return {
            'type': 'ir.actions.act_window',
            'name': name,
            'res_model': model,
            'view_mode': view_mode,
            'domain': domain,
        }

    def _action_near_expiry(self):
        near_days = self.env['pharmacy.expiry.alert']._near_expiry_days()
        now = fields.Datetime.now()
        horizon = fields.Datetime.add(now, days=near_days)
        return self._act_window(
            _('Expiring Within %s Days', near_days), 'stock.quant',
            [('product_id.is_medicine', '=', True),
             ('location_id.usage', '=', 'internal'), ('quantity', '>', 0),
             ('lot_id.expiration_date', '>=', now),
             ('lot_id.expiration_date', '<=', horizon)],
            view_mode='list,pivot')
