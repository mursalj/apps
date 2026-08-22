# -*- coding: utf-8 -*-
"""Service account — the spine of the meter-to-cash cycle (SRS 5.2, 6).

A customer is a res.partner. What the utility actually bills is a SERVICE ACCOUNT: one
connection, at one address, for one utility, with its own account number, tariff, billing
cycle, deposit and balance. "ABC Hotel" is one partner with three service accounts
(electricity, water, a second property), each carrying its own meter history and ledger.

Everything downstream keys on this record rather than on the partner:
readings roll up to it, the billing run produces one bill per account, the statement is
per account, and connection status (SRS 6) lives here — a meter has a physical state
(working / faulty), an account has a commercial one (active / suspended / disconnected).
"""

import operator as operator_module

from dateutil.relativedelta import relativedelta

from odoo import models, fields, api, _
from odoo.exceptions import ValidationError, UserError

from .utility_tariff import UTILITY_TYPES

# Account-number prefix per utility, so a number is readable at a glance on a bill.
UTILITY_CODE = {
    'water': 'WTR',
    'electricity': 'ELE',
    'gas': 'GAS',
    'internet': 'NET',
}

OPERATORS = {
    '=': operator_module.eq, '!=': operator_module.ne,
    '<': operator_module.lt, '<=': operator_module.le,
    '>': operator_module.gt, '>=': operator_module.ge,
}

# Commercial states that still consume the service, i.e. that a billing run must pick up.
BILLABLE_STATES = ('active', 'suspended')


class UtilityServiceAccount(models.Model):
    _name = 'utility.service.account'
    _description = 'Utility Service Account'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'utility.sequence.mixin']
    _order = 'name'

    name = fields.Char('Account No.', required=True, copy=False, readonly=True,
                       default=lambda s: _('New'), index=True, tracking=True)
    partner_id = fields.Many2one('res.partner', string='Customer', required=True,
                                 ondelete='restrict', index=True, tracking=True)
    utility_type = fields.Selection(UTILITY_TYPES, string='Utility', required=True,
                                    tracking=True)
    state = fields.Selection([
        ('pending', 'Pending'),
        ('active', 'Active'),
        ('suspended', 'Suspended'),
        ('disconnected', 'Disconnected'),
        ('closed', 'Closed'),
        ('transferred', 'Transferred'),
    ], default='pending', required=True, tracking=True,
        help='Commercial status of the connection. Suspended still bills (the service is '
             'withheld, not terminated); Disconnected and Closed do not.')

    # --- service location (SRS 6) ---
    street = fields.Char('Service Address')
    city = fields.Char('City')
    area = fields.Char('Area / Zone',
                       help='Geographic zone used for routing and for area-based tariffs '
                            'and reports.')
    latitude = fields.Float('Latitude', digits=(10, 7))
    longitude = fields.Float('Longitude', digits=(10, 7))

    # --- commercial terms ---
    tariff_id = fields.Many2one('utility.tariff', string='Tariff', tracking=True,
                                help='Default tariff for every meter on this account. A '
                                     'meter may override it.')
    billing_cycle_id = fields.Many2one('utility.billing.cycle', string='Billing Cycle',
                                       tracking=True)
    connection_date = fields.Date('Connection Date', tracking=True)
    disconnection_date = fields.Date('Disconnection Date', readonly=True, copy=False)
    deposit_amount = fields.Monetary('Security Deposit', currency_field='currency_id',
                                     tracking=True)
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id',
                                  readonly=True)
    last_billed_date = fields.Date('Last Billed', readonly=True, copy=False,
                                   help='End of the last period billed on this account. '
                                        'Drives the billing cycle.')

    # --- estimated billing (SRS 14) ---
    estimation_method = fields.Selection([
        ('none', 'Do Not Estimate'),
        ('previous', 'Previous Period'),
        ('avg3', 'Average of Last 3'),
        ('avg6', 'Average of Last 6'),
        ('seasonal', 'Same Period Last Year'),
        ('minimum', 'Configured Minimum'),
    ], string='Estimation Method', default='avg3', required=True,
        help='How consumption is estimated when a meter cannot be read. "Do Not Estimate" '
             'leaves the account unbilled for that period rather than guessing.')
    minimum_consumption = fields.Float(
        'Minimum Consumption', help='Used by the Minimum method, and as the floor when an '
                                    'average cannot be calculated.')

    # --- links ---
    meter_ids = fields.One2many('utility.meter', 'account_id', string='Meters')
    installation_ids = fields.One2many('utility.meter.installation', 'account_id',
                                       string='Meter History')
    reading_ids = fields.One2many('utility.meter.reading', 'account_id', string='Readings')
    meter_count = fields.Integer(compute='_compute_counts')
    reading_count = fields.Integer(compute='_compute_counts')
    invoice_count = fields.Integer(compute='_compute_financials')
    balance = fields.Monetary('Outstanding Balance', currency_field='currency_id',
                              compute='_compute_financials', search='_search_balance',
                              help='Unpaid amount across posted invoices for this account.')

    # --- communication preferences (SRS 22) ---
    notify_email = fields.Boolean('Notify by Email', default=True)
    notify_whatsapp = fields.Boolean('Notify by WhatsApp', default=False,
                                     help='Requires the Meta WhatsApp Cloud API to be '
                                          'configured; until then these are logged as '
                                          'skipped rather than silently dropped.')
    notification_phone = fields.Char('Notification Number',
                                     help='Overrides the customer\'s phone for messaging. '
                                          'Include the country code.')
    notification_ids = fields.One2many('utility.notification', 'account_id',
                                       string='Notifications')

    active = fields.Boolean(default=True)
    note = fields.Text('Notes')
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company,
                                 required=True)

    _sql_constraints = [
        ('name_uniq', 'unique(name, company_id)',
         'That service account number already exists.'),
    ]

    # ------------------------------------------------------------------
    # Compute
    # ------------------------------------------------------------------
    @api.depends('meter_ids', 'reading_ids')
    def _compute_counts(self):
        # read_group rather than len(record.meter_ids): an account with years of readings
        # must not pull them all into memory to show a stat button.
        meters = {a.id: c for a, c in self.env['utility.meter']._read_group(
            [('account_id', 'in', self.ids)], ['account_id'], ['__count'])}
        readings = {a.id: c for a, c in self.env['utility.meter.reading']._read_group(
            [('account_id', 'in', self.ids)], ['account_id'], ['__count'])}
        for account in self:
            key = account._origin.id or account.id
            account.meter_count = meters.get(key, 0)
            account.reading_count = readings.get(key, 0)

    @api.depends('company_id')
    def _compute_financials(self):
        """Outstanding balance from posted customer invoices stamped with this account.

        Aggregated with sudo() over an explicit account_id filter. The recordset is
        avoids the failure recorded in PROD_PENDING_CHANGES.md section 9, where reading an
        """
        totals = {}
        counts = {}
        if self.ids:
            groups = self.env['account.move'].sudo()._read_group(
                [('utility_account_id', 'in', self.ids),
                 ('move_type', 'in', ('out_invoice', 'out_refund')),
                 ('state', '=', 'posted')],
                ['utility_account_id'], ['amount_residual_signed:sum', '__count'])
            for account, residual, count in groups:
                totals[account.id] = residual
                counts[account.id] = count
        for account in self:
            key = account._origin.id or account.id
            account.balance = totals.get(key, 0.0)
            account.invoice_count = counts.get(key, 0)

    def _search_balance(self, operator, value):
        """Make the computed balance filterable ("accounts owing money").

        Aggregates the residual per account once and returns the matching ids, rather
        than making the field stored — a stored balance would need invalidating on every
        invoice, payment and reconciliation, and would still drift.
        """
        groups = self.env['account.move'].sudo()._read_group(
            [('utility_account_id', '!=', False),
             ('move_type', 'in', ('out_invoice', 'out_refund')),
             ('state', '=', 'posted')],
            ['utility_account_id'], ['amount_residual_signed:sum'])
        balances = {account.id: residual for account, residual in groups}
        matching = [
            account_id for account_id, residual in balances.items()
            if OPERATORS[operator](residual, value)
        ]
        if operator in ('=', '<=', '<') or (operator == '!=' and value):
            # Accounts with no posted invoice have a balance of zero, which these
            # operators can match — they are absent from the aggregate entirely.
            zero_matches = OPERATORS[operator](0.0, value)
            if zero_matches:
                billed = set(balances)
                matching += [a.id for a in self.search([]) if a.id not in billed]
        return [('id', 'in', matching)]

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    @api.constrains('tariff_id', 'utility_type')
    def _check_tariff_type(self):
        for account in self:
            if account.tariff_id and account.tariff_id.utility_type != account.utility_type:
                raise ValidationError(_(
                    "Tariff '%(t)s' is for %(tt)s but account %(a)s supplies %(at)s.",
                    t=account.tariff_id.name, tt=account.tariff_id.utility_type,
                    a=account.name, at=account.utility_type))

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                vals['name'] = self._next_account_number(vals.get('utility_type'))
        return super().create(vals_list)

    @api.model
    def _next_account_number(self, utility_type):
        """ELE/2026/000042 — the utility is visible in the number itself."""
        code = UTILITY_CODE.get(utility_type, 'UTL')
        number = self._next_reference('utility.service.account') or '0001'
        return '%s/%s' % (code, number)

    def copy(self, default=None):
        default = dict(default or {})
        default.setdefault('name', _('New'))
        return super().copy(default)

    # ------------------------------------------------------------------
    # Estimation (SRS 14)
    # ------------------------------------------------------------------
    def _estimate_consumption(self, meter, on_date=None):
        """Best estimate of a meter's consumption for a period it was not read in.

        Every method falls back to the one below it rather than returning nothing: a
        meter with no history at all still has to produce a defensible figure, and the
        configured minimum is the utility's own answer to that. Returns 0.0 only when the
        account is explicitly set not to estimate.
        """
        self.ensure_one()
        if self.estimation_method == 'none':
            return 0.0
        Reading = self.env['utility.meter.reading'].sudo()
        history = Reading.search(
            [('meter_id', '=', meter.id), ('state', 'in', ('validated', 'billed')),
             ('reading_type', '!=', 'estimated'), ('consumption', '>', 0)],
            order='reading_date desc', limit=12)

        estimate = 0.0
        if self.estimation_method == 'previous' and history:
            estimate = history[0].consumption
        elif self.estimation_method in ('avg3', 'avg6') and history:
            window = 3 if self.estimation_method == 'avg3' else 6
            sample = history[:window]
            estimate = sum(sample.mapped('consumption')) / len(sample)
        elif self.estimation_method == 'seasonal':
            estimate = self._seasonal_estimate(meter, on_date)
            if not estimate and history:
                # No reading from this month last year — fall back to a recent average
                # rather than billing zero for a genuinely consuming meter.
                sample = history[:3]
                estimate = sum(sample.mapped('consumption')) / len(sample)
        elif self.estimation_method == 'minimum':
            estimate = self.minimum_consumption

        return max(estimate, self.minimum_consumption or 0.0)

    def _seasonal_estimate(self, meter, on_date=None):
        """Consumption in the same calendar month a year ago — the seasonal method."""
        self.ensure_one()
        on_date = on_date or fields.Date.today()
        target = on_date - relativedelta(years=1)
        start = target.replace(day=1)
        end = start + relativedelta(months=1)
        readings = self.env['utility.meter.reading'].sudo().search([
            ('meter_id', '=', meter.id),
            ('state', 'in', ('validated', 'billed')),
            ('reading_type', '!=', 'estimated'),
            ('reading_date', '>=', fields.Datetime.to_datetime(start)),
            ('reading_date', '<', fields.Datetime.to_datetime(end)),
        ])
        return sum(readings.mapped('consumption')) if readings else 0.0

    def _meters_missing_reading(self, period_start, period_end):
        """Meters on this account with no usable reading inside the period (SRS 9).

        This is the "missing reading" check: it is a question about a PERIOD, so it cannot
        live on the reading model — the whole point is that no reading exists.
        """
        self.ensure_one()
        Reading = self.env['utility.meter.reading'].sudo()
        missing = self.env['utility.meter']
        for meter in self.meter_ids.filtered(lambda m: m.state == 'active'):
            found = Reading.search_count([
                ('meter_id', '=', meter.id),
                ('state', '!=', 'cancel'),
                ('exception_state', 'not in', ('invalid',)),
                ('reading_date', '>=', fields.Datetime.to_datetime(period_start)),
                ('reading_date', '<=', fields.Datetime.to_datetime(period_end).replace(
                    hour=23, minute=59, second=59)),
            ])
            if not found:
                missing |= meter
        return missing

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def action_activate(self):
        for account in self:
            if not account.tariff_id:
                raise UserError(_(
                    "Account %s has no tariff, so it cannot be activated — a billing run "
                    "would have nothing to charge.", account.name))
            account.write({
                'state': 'active',
                'connection_date': account.connection_date or fields.Date.today(),
            })

    def action_suspend(self):
        self.write({'state': 'suspended'})

    def action_disconnect(self):
        self.write({'state': 'disconnected',
                    'disconnection_date': fields.Date.today()})

    def action_close(self):
        for account in self:
            if account.balance > 0:
                account.message_post(body=_(
                    "Account closed with an outstanding balance of %s.", account.balance))
        self.write({'state': 'closed', 'disconnection_date': fields.Date.today()})

    def action_reset_pending(self):
        self.write({'state': 'pending'})

    # ------------------------------------------------------------------
    # Notifications (SRS 22)
    # ------------------------------------------------------------------
    def _notification_channels(self):
        self.ensure_one()
        channels = []
        if self.notify_email:
            channels.append('email')
        if self.notify_whatsapp:
            channels.append('whatsapp')
        return channels

    def _notification_phone(self):
        self.ensure_one()
        # Odoo 19 merged res.partner.mobile into phone; there is no separate mobile field.
        return self.notification_phone or self.partner_id.phone

    def notify(self, template_key, record=None):
        """Send one notification per opted-in channel; returns the log rows."""
        self.ensure_one()
        return self.env['utility.notification'].notify(self, template_key, record=record)

    def action_send_statement(self):
        """Email the current statement to the customer."""
        self.ensure_one()
        return self.notify('overdue' if self.balance > 0 else 'invoice_issued')

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def action_view_meters(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Meters — %s', self.name),
            'res_model': 'utility.meter',
            'view_mode': 'list,form',
            'domain': [('account_id', '=', self.id)],
            'context': {'default_account_id': self.id,
                        'default_utility_type': self.utility_type},
        }

    def action_view_readings(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Readings — %s', self.name),
            'res_model': 'utility.meter.reading',
            'view_mode': 'list,form',
            'domain': [('account_id', '=', self.id)],
        }

    def action_view_invoices(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Invoices — %s', self.name),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('utility_account_id', '=', self.id),
                       ('move_type', 'in', ('out_invoice', 'out_refund'))],
            'context': {'default_move_type': 'out_invoice',
                        'default_partner_id': self.partner_id.id},
        }

    def _display_name_parts(self):
        self.ensure_one()
        return '%s — %s' % (self.name, self.partner_id.display_name or '')

    @api.depends('name', 'partner_id')
    def _compute_display_name(self):
        for account in self:
            account.display_name = account._display_name_parts()
