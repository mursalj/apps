# -*- coding: utf-8 -*-
"""Management dashboard data (SRS 25).

A single RPC returns every figure the birdseye view renders: headline KPIs, monthly
consumption and revenue series, the meter mix, and the readings needing attention. All

Revenue is aggregated with _read_group over account.move. The previous implementation
searched every billed reading and mapped to its invoice, which loaded the whole reading
table into memory to reach a handful of invoices — and, worse, dereferenced an invoice
recorded in docs/PROD_PENDING_CHANGES.md section 9.
"""

from dateutil.relativedelta import relativedelta

from odoo import models, fields, api, _


class UtilityDashboard(models.TransientModel):
    _name = 'utility.dashboard'
    _description = 'Utility Management Dashboard'

    @api.model
    def get_dashboard_data(self, months=12):
        months = max(1, min(int(months or 12), 24))
        Meter = self.env['utility.meter']
        Reading = self.env['utility.meter.reading']
        today = fields.Date.today()
        currency = self.env.company.currency_id

        meters = Meter.search([])
        active = meters.filtered(lambda m: m.state == 'active')

        # --- month buckets, oldest first ---
        first_this = today.replace(day=1)
        buckets = []
        cursor = first_this - relativedelta(months=months - 1)
        while cursor <= first_this:
            nxt = cursor + relativedelta(months=1)
            buckets.append({'key': cursor.strftime('%Y-%m'),
                            'label': cursor.strftime('%b %Y'),
                            'start': cursor, 'end': nxt,
                            'consumption': 0.0, 'revenue': 0.0})
            cursor = nxt
        by_key = {b['key']: b for b in buckets}
        window_start = buckets[0]['start']

        def bucket_for(d):
            return by_key.get(d.strftime('%Y-%m')) if d else None

        # --- consumption per month (from readings) ---
        readings = Reading.search([
            ('state', 'in', ('validated', 'billed')),
            ('reading_date', '>=', fields.Datetime.to_datetime(window_start)),
        ])
        for r in readings:
            b = bucket_for(fields.Date.to_date(r.reading_date))
            if b:
                b['consumption'] += r.consumption

        # --- revenue per month (utility invoices carry the service account) ---
        Move = self.env['account.move']
        invoice_domain = [
            ('utility_account_id', '!=', False),
            ('move_type', 'in', ('out_invoice', 'out_refund')),
            ('state', '=', 'posted'),
        ]
        revenue_groups = Move._read_group(
            invoice_domain + [('invoice_date', '>=', window_start)],
            ['invoice_date:month'], ['amount_total_signed:sum'])
        for month, amount in revenue_groups:
            b = bucket_for(month)
            if b:
                b['revenue'] += amount

        # --- headline KPIs ---
        high_usage = Reading.search_count([
            ('is_high_usage', '=', True), ('state', 'in', ('draft', 'validated'))])
        unbilled = Reading.search_count([('state', '=', 'validated')])
        month_start = fields.Datetime.to_datetime(first_this)
        readings_this_month = Reading.search_count([
            ('reading_date', '>=', month_start), ('state', '!=', 'cancel')])
        this_bucket = buckets[-1]
        [(outstanding, billed_total)] = Move._read_group(
            invoice_domain, [], ['amount_residual_signed:sum', 'amount_total_signed:sum'])
        outstanding = outstanding or 0.0
        billed_total = billed_total or 0.0

        # --- SRS 25 / 48 completion and quality rates ---
        Account = self.env['utility.service.account']
        accounts_billable = Account.search_count([('state', 'in', ('active', 'suspended'))])
        billed_this_month = Move.search_count(
            invoice_domain + [('invoice_date', '>=', first_this)])
        active_meter_count = len(active)
        read_this_month = len(Reading.search([
            ('reading_date', '>=', month_start), ('state', '!=', 'cancel')]).mapped('meter_id'))
        estimated_this_month = Reading.search_count([
            ('reading_date', '>=', month_start), ('state', '!=', 'cancel'),
            ('reading_type', '=', 'estimated')])
        open_exceptions = Reading.search_count([
            ('exception_state', 'in', ('review', 'invalid', 'tampering')),
            ('reviewed_by', '=', False)])
        runs_awaiting = self.env['utility.billing.run'].search_count([
            ('state', '=', 'review')])

        def rate(part, whole):
            return round(100.0 * part / whole, 1) if whole else 0.0

        kpis = {
            'meters_total': len(meters),
            'meters_active': active_meter_count,
            'meters_maintenance': len(meters.filtered(
                lambda m: m.state in ('maintenance', 'faulty'))),
            'customers': len(meters.mapped('partner_id')),
            'accounts': accounts_billable,
            'readings_this_month': readings_this_month,
            'high_usage': high_usage,
            'unbilled': unbilled,
            'revenue_this_month': this_bucket['revenue'],
            'outstanding': outstanding,
            # Completion and quality: the SRS 48 success metrics.
            'reading_completion': rate(read_this_month, active_meter_count),
            'billing_completion': rate(billed_this_month, accounts_billable),
            'estimated_share': rate(estimated_this_month, readings_this_month),
            'collection_rate': rate(billed_total - outstanding, billed_total),
            'open_exceptions': open_exceptions,
            'runs_awaiting_review': runs_awaiting,
        }

        # --- meter mix by utility type ---
        type_labels = dict(Meter._fields['utility_type'].selection)
        mix = {}
        for m in meters:
            mix[m.utility_type] = mix.get(m.utility_type, 0) + 1
        meter_mix = [{'label': type_labels.get(k, k), 'value': v}
                     for k, v in sorted(mix.items())]

        # --- attention: readings the validation engine could not clear ---
        attention = [{
            'id': r.id,
            'name': r.name,
            'meter': r.meter_id.name,
            'customer': r.partner_id.display_name or '',
            'consumption': r.consumption,
            'average': round(r.baseline_average, 2),
            'exception': r.exception_state,
            'codes': r.exception_codes or '',
        } for r in Reading.search([
            ('exception_state', 'in', ('review', 'invalid', 'tampering')),
            ('reviewed_by', '=', False),
            ('state', 'in', ('draft', 'validated')),
        ], order='reading_date desc', limit=8)]

        return {
            'currency': {'symbol': currency.symbol or '',
                         'position': currency.position or 'before',
                         'decimals': currency.decimal_places},
            'kpis': kpis,
            'series': [{'label': b['label'],
                        'consumption': round(b['consumption'], 2),
                        'revenue': round(b['revenue'], 2)} for b in buckets],
            'meter_mix': meter_mix,
            'attention': attention,
        }

    @api.model
    def action_open_high_usage(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Readings Awaiting Review'),
            'res_model': 'utility.meter.reading',
            'view_mode': 'list,form',
            'domain': [('exception_state', 'in', ('review', 'invalid', 'tampering')),
                       ('reviewed_by', '=', False)],
        }

    @api.model
    def action_open_runs_for_review(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Billing Runs Awaiting Review'),
            'res_model': 'utility.billing.run',
            'view_mode': 'list,form',
            'domain': [('state', '=', 'review')],
        }

    @api.model
    def action_open_reading(self, reading_id):
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'utility.meter.reading',
            'res_id': int(reading_id),
            'view_mode': 'form',
        }
