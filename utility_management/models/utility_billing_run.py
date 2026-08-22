# -*- coding: utf-8 -*-
"""Billing run - the controlled path from readings to invoices (SRS 12, 34, 35).

Before this existed, invoices were created straight out of a wizard: no period, no
preview, no approval, no way to reproduce what a previous month actually did, and no way
to notice that this month's billing had collapsed to a third of last month's before the
bills went out.

A run fixes that by making the calculation a RECORD:

    draft -> review -> approved -> invoiced

* **Compute** resolves, for every meter on every billable account, which reading covers
  the period, what it consumed and what that costs - and writes one line per meter. It is
  idempotent: recomputing a draft run rebuilds the same lines from the same readings, so
  a run is reproducible (SRS 47.12).
* **Review** is where a human looks at the totals and at the warnings the run raises
  against the previous period (SRS 35).
* **Approval** locks the readings the run used, so nobody edits a figure out from under an
  approved bill.
* **Invoicing** is the only step that touches account.move, and it cannot run twice: a
  line that already carries an invoice is skipped.
"""

import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

from .utility_service_account import BILLABLE_STATES
from .utility_tariff import UTILITY_TYPES

_logger = logging.getLogger(__name__)

# SRS 35 thresholds. Deliberately generous: a warning that fires every month is noise, and
# noise is what makes people click through the one that mattered.
BILLING_DROP_ALERT = 0.30        # total billed down more than 30% on the previous run
CONSUMPTION_JUMP_ALERT = 0.30    # total consumption up more than 30%
ESTIMATE_SHARE_ALERT = 0.20      # more than a fifth of lines estimated
ZERO_SHARE_ALERT = 0.10          # more than a tenth of meters reading zero


class UtilityBillingRun(models.Model):
    _name = 'utility.billing.run'
    _description = 'Utility Billing Run'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'utility.sequence.mixin']
    _order = 'period_end desc, id desc'

    name = fields.Char('Reference', default=lambda s: _('New'), copy=False, readonly=True,
                       index=True)
    period_start = fields.Date('Period From', required=True, tracking=True)
    period_end = fields.Date('Period To', required=True, tracking=True)
    billing_cycle_id = fields.Many2one('utility.billing.cycle', string='Billing Cycle',
                                       tracking=True,
                                       help='Only accounts on this cycle are included. '
                                            'Empty means every cycle.')
    utility_type = fields.Selection(UTILITY_TYPES, string='Utility',
                                    help='Empty means every utility.')
    account_ids = fields.Many2many('utility.service.account', string='Only These Accounts',
                                   help='Empty means every billable account.')

    state = fields.Selection([
        ('draft', 'Draft'),
        ('review', 'Under Review'),
        ('approved', 'Approved'),
        ('invoiced', 'Invoiced'),
        ('cancel', 'Cancelled'),
    ], default='draft', required=True, tracking=True)

    create_estimates = fields.Boolean(
        'Estimate Missing Readings', default=False, tracking=True,
        help='Write an estimated reading for any meter with no actual reading in the '
             'period, using the account\'s estimation method (SRS 14). Off by default: '
             'estimating is a decision, not a default.')
    apply_penalties = fields.Boolean(
        'Apply Late Penalties', default=False, tracking=True,
        help='Add the tariff\'s penalty charges for accounts carrying an overdue balance.')

    line_ids = fields.One2many('utility.billing.run.line', 'run_id', string='Lines')
    invoice_ids = fields.One2many('account.move', 'utility_billing_run_id',
                                  string='Invoices')

    # --- totals, computed from the lines so a review cannot see stale figures ---
    line_count = fields.Integer(compute='_compute_totals', store=True)
    account_count = fields.Integer(compute='_compute_totals', store=True)
    total_consumption = fields.Float(compute='_compute_totals', store=True)
    total_amount = fields.Monetary(compute='_compute_totals', store=True,
                                   currency_field='currency_id')
    estimated_count = fields.Integer(compute='_compute_totals', store=True)
    exception_count = fields.Integer(compute='_compute_totals', store=True)
    zero_count = fields.Integer(compute='_compute_totals', store=True)
    invoice_count = fields.Integer(compute='_compute_invoice_count')
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id',
                                  readonly=True)

    # --- comparison against the previous run (SRS 35) ---
    previous_run_id = fields.Many2one('utility.billing.run', string='Compared With',
                                      readonly=True, copy=False)
    variance_percent = fields.Float('Variance vs Previous (%)', readonly=True, copy=False)
    warning_text = fields.Text('Review Warnings', readonly=True, copy=False)

    prepared_by = fields.Many2one('res.users', string='Prepared By', readonly=True,
                                  copy=False)
    prepared_on = fields.Datetime('Prepared On', readonly=True, copy=False)
    approved_by = fields.Many2one('res.users', string='Approved By', readonly=True,
                                  copy=False)
    approved_on = fields.Datetime('Approved On', readonly=True, copy=False)
    note = fields.Text('Notes')
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company,
                                 required=True)

    @api.constrains('period_start', 'period_end')
    def _check_period(self):
        for run in self:
            if run.period_end < run.period_start:
                raise ValidationError(_("A billing period cannot end before it starts."))

    @api.depends('line_ids.amount', 'line_ids.consumption', 'line_ids.is_estimated',
                 'line_ids.account_id', 'line_ids.exception_state')
    def _compute_totals(self):
        for run in self:
            lines = run.line_ids
            run.line_count = len(lines)
            run.account_count = len(lines.mapped('account_id'))
            run.total_consumption = sum(lines.mapped('consumption'))
            run.total_amount = sum(lines.mapped('amount'))
            run.estimated_count = len(lines.filtered('is_estimated'))
            run.exception_count = len(lines.filtered(
                lambda l: l.exception_state not in ('normal', 'estimated')))
            run.zero_count = len(lines.filtered(lambda l: not l.consumption))

    def _compute_invoice_count(self):
        for run in self:
            run.invoice_count = len(run.invoice_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                vals['name'] = self._next_reference('utility.billing.run') or _('New')
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Compute
    # ------------------------------------------------------------------
    def action_compute(self):
        """(Re)build the run's lines from the readings in the period.

        Wipes and rebuilds rather than patching: a run whose lines were half-updated is
        exactly the sort of thing nobody can audit six months later. Only draft runs can
        be recomputed, so an approved run is a permanent record of what was billed.
        """
        for run in self:
            if run.state not in ('draft', 'review'):
                raise UserError(_(
                    "Run %s is %s. Only a draft run can be recomputed.",
                    run.name, run.state))
            run.line_ids.unlink()
            values = []
            for account in run._billable_accounts():
                values += run._lines_for_account(account)
            self.env['utility.billing.run.line'].create(values)
            run.write({
                'state': 'review',
                'prepared_by': self.env.user.id,
                'prepared_on': fields.Datetime.now(),
            })
            run._evaluate_warnings()
            run.message_post(body=_(
                "Computed %(lines)s line(s) across %(accounts)s account(s): %(amount)s.",
                lines=run.line_count, accounts=run.account_count,
                amount=run.total_amount))
        return True

    def _billable_accounts(self):
        self.ensure_one()
        domain = [('state', 'in', BILLABLE_STATES), ('company_id', '=', self.company_id.id)]
        if self.billing_cycle_id:
            domain.append(('billing_cycle_id', '=', self.billing_cycle_id.id))
        if self.utility_type:
            domain.append(('utility_type', '=', self.utility_type))
        if self.account_ids:
            domain.append(('id', 'in', self.account_ids.ids))
        return self.env['utility.service.account'].search(domain)

    def _lines_for_account(self, account):
        """One line per active meter on the account - estimating only if asked to."""
        self.ensure_one()
        Reading = self.env['utility.meter.reading']
        start = fields.Datetime.to_datetime(self.period_start)
        end = fields.Datetime.to_datetime(self.period_end).replace(
            hour=23, minute=59, second=59)
        values = []
        for meter in account.meter_ids.filtered(lambda m: m.state == 'active'):
            readings = Reading.search([
                ('meter_id', '=', meter.id),
                ('state', '=', 'validated'),
                ('superseded', '=', False),
                ('invoice_id', '=', False),
                ('reading_date', '>=', start),
                ('reading_date', '<=', end),
            ], order='reading_date asc')
            if not readings and self.create_estimates:
                estimate = Reading._create_estimated_reading(meter, self.period_end)
                estimate.action_validate()
                readings = estimate
            for reading in readings:
                values.append(self._line_values(account, meter, reading))
            if not readings:
                values.append(self._line_values(account, meter, Reading))
        return values

    def _line_values(self, account, meter, reading):
        """A line, whether or not a reading was found - a gap has to be visible."""
        self.ensure_one()
        tariff = meter.effective_tariff_id
        consumption = reading.consumption if reading else 0.0
        amount = 0.0
        if tariff and reading and consumption >= 0:
            amount = sum(line['subtotal'] for line in tariff.compute_charges(
                consumption, apply_penalties=self.apply_penalties,
                overdue_amount=account.balance))
        elif tariff and reading and consumption < 0:
            amount = consumption * (tariff.flat_rate or 0.0)
        return {
            'run_id': self.id,
            'account_id': account.id,
            'meter_id': meter.id,
            'reading_id': reading.id if reading else False,
            'tariff_id': tariff.id if tariff else False,
            'previous_reading': reading.previous_reading if reading else 0.0,
            'present_reading': reading.present_reading if reading else 0.0,
            'consumption': consumption,
            'amount': amount,
            'is_estimated': bool(reading) and reading.reading_type == 'estimated',
            'exception_state': reading.exception_state if reading else 'review',
            'note': '' if reading else _('No reading in this period.'),
        }

    # ------------------------------------------------------------------
    # Review warnings (SRS 35)
    # ------------------------------------------------------------------
    def _evaluate_warnings(self):
        """Compare with the previous comparable run and raise the SRS 35 flags."""
        for run in self:
            previous = self.search([
                ('id', '!=', run.id),
                ('state', 'in', ('approved', 'invoiced')),
                ('company_id', '=', run.company_id.id),
                ('utility_type', '=', run.utility_type),
                ('period_end', '<', run.period_start),
            ], order='period_end desc', limit=1)
            warnings = []

            if previous and previous.total_amount:
                variance = (run.total_amount - previous.total_amount) / previous.total_amount
                run.variance_percent = variance * 100
                if variance <= -BILLING_DROP_ALERT:
                    warnings.append(_(
                        "Billing is down %(pct).1f%% on %(prev)s (%(now)s vs %(then)s). "
                        "Check for missing readings before approving.",
                        pct=abs(variance) * 100, prev=previous.name,
                        now=run.total_amount, then=previous.total_amount))
                if previous.total_consumption and run.total_consumption > \
                        previous.total_consumption * (1 + CONSUMPTION_JUMP_ALERT):
                    warnings.append(_(
                        "Total consumption is more than %(pct)s%% above %(prev)s.",
                        pct=int(CONSUMPTION_JUMP_ALERT * 100), prev=previous.name))
            run.previous_run_id = previous.id if previous else False

            if run.line_count:
                if run.estimated_count / run.line_count > ESTIMATE_SHARE_ALERT:
                    warnings.append(_(
                        "%(n)s of %(total)s lines are estimated readings.",
                        n=run.estimated_count, total=run.line_count))
                if run.zero_count / run.line_count > ZERO_SHARE_ALERT:
                    warnings.append(_(
                        "%(n)s meters recorded zero consumption.",
                        n=run.zero_count, total=run.line_count))
            if run.exception_count:
                warnings.append(_(
                    "%(n)s line(s) rest on a reading with an unresolved exception and "
                    "will NOT be invoiced.", n=run.exception_count))
            missing = len(run.line_ids.filtered(lambda l: not l.reading_id))
            if missing:
                warnings.append(_(
                    "%(n)s meter(s) had no reading at all in this period.", n=missing))

            run.warning_text = '\n'.join('* %s' % w for w in warnings) or False

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def _assert_may_approve(self):
        if not self.env.user.has_group('utility_management.group_utility_manager'):
            raise UserError(_(
                "Approving a billing run is a Utility Manager decision - it is the "
                "control that stands between a miscalculated run and the customer."))

    def action_approve(self):
        """Approve the run and LOCK the readings behind it."""
        self._assert_may_approve()
        for run in self:
            if run.state != 'review':
                raise UserError(_("Only a computed run under review can be approved."))
            if not run.line_ids:
                raise UserError(_("Run %s has no lines to approve.", run.name))
            run.line_ids.mapped('reading_id').write({'billing_run_id': run.id})
            run.write({
                'state': 'approved',
                'approved_by': self.env.user.id,
                'approved_on': fields.Datetime.now(),
            })
            run.message_post(body=_(
                "Approved: %(amount)s across %(accounts)s account(s). Readings are now "
                "locked.", amount=run.total_amount, accounts=run.account_count))

    def action_generate_invoices(self):
        """Turn approved lines into one draft invoice per service account."""
        for run in self:
            if run.state != 'approved':
                raise UserError(_(
                    "Run %s must be approved before invoices are generated.", run.name))
            invoices = self.env['account.move']
            for account in run.line_ids.mapped('account_id'):
                lines = run.line_ids.filtered(
                    lambda l: l.account_id == account and l.is_invoiceable())
                if not lines:
                    continue
                readings = lines.mapped('reading_id')
                account_invoices = readings.with_context(
                    utility_billing_run_id=run.id,
                    utility_apply_penalties=run.apply_penalties)._bill()
                for invoice in account_invoices:
                    invoice.utility_billing_run_id = run.id
                lines.write({'invoice_id': account_invoices[:1].id})
                invoices |= account_invoices
            run.state = 'invoiced'
            run.message_post(body=_("%s invoice(s) generated.", len(invoices)))
        return self.action_view_invoices()

    def action_cancel(self):
        for run in self:
            if run.state == 'invoiced':
                raise UserError(_(
                    "Run %s has already produced invoices. Reverse those invoices "
                    "instead - a billed period is not cancelled, it is corrected.",
                    run.name))
            run.line_ids.mapped('reading_id').write({'billing_run_id': False})
            run.state = 'cancel'

    def action_reset_draft(self):
        for run in self:
            if run.state == 'invoiced':
                raise UserError(_("An invoiced run cannot be reopened."))
            run.line_ids.mapped('reading_id').write({'billing_run_id': False})
            run.write({'state': 'draft', 'approved_by': False, 'approved_on': False})

    def action_notify_customers(self):
        """Send each invoice of this run to its customer, on their chosen channels.

        Deliberately a button, not a step of action_generate_invoices: generating drafts
        is reversible, contacting customers is not. Nothing here runs on a schedule.
        """
        self.ensure_one()
        if self.state != 'invoiced':
            raise UserError(_("Generate the invoices before notifying customers."))
        notifications = self.env['utility.notification']
        for invoice in self.invoice_ids:
            account = invoice.utility_account_id
            if not account:
                continue
            key = 'estimated_bill' if invoice.utility_is_estimated else 'invoice_issued'
            notifications |= account.notify(key, record=invoice)
        sent = len(notifications.filtered(lambda n: n.state == 'sent'))
        skipped = len(notifications.filtered(lambda n: n.state == 'skipped'))
        self.message_post(body=_(
            "Customer notifications: %(sent)s sent, %(skipped)s skipped.",
            sent=sent, skipped=skipped))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Notifications - %s', self.name),
            'res_model': 'utility.notification',
            'view_mode': 'list',
            'domain': [('id', 'in', notifications.ids)],
        }

    def action_view_invoices(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Invoices - %s', self.name),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('utility_billing_run_id', '=', self.id)],
            'context': {'default_move_type': 'out_invoice'},
        }


class UtilityBillingRunLine(models.Model):
    _name = 'utility.billing.run.line'
    _description = 'Utility Billing Run Line'
    _order = 'account_id, meter_id'

    run_id = fields.Many2one('utility.billing.run', required=True, ondelete='cascade',
                             index=True)
    account_id = fields.Many2one('utility.service.account', string='Service Account',
                                 required=True, ondelete='restrict', index=True)
    partner_id = fields.Many2one(related='account_id.partner_id', store=True, readonly=True)
    meter_id = fields.Many2one('utility.meter', string='Meter', required=True,
                               ondelete='restrict')
    reading_id = fields.Many2one('utility.meter.reading', string='Reading',
                                 ondelete='restrict')
    tariff_id = fields.Many2one('utility.tariff', string='Tariff')
    previous_reading = fields.Float('Previous')
    present_reading = fields.Float('Present')
    consumption = fields.Float('Consumption')
    amount = fields.Monetary('Amount', currency_field='currency_id')
    currency_id = fields.Many2one(related='run_id.currency_id', readonly=True)
    is_estimated = fields.Boolean('Estimated')
    exception_state = fields.Selection([
        ('normal', 'Normal'),
        ('review', 'Review Required'),
        ('estimated', 'Estimated'),
        ('invalid', 'Invalid'),
        ('tampering', 'Suspected Tampering'),
    ], default='normal')
    invoice_id = fields.Many2one('account.move', string='Invoice', readonly=True)
    note = fields.Char('Note')
    company_id = fields.Many2one(related='run_id.company_id', store=True, readonly=True)

    _sql_constraints = [
        ('run_meter_reading_uniq', 'unique(run_id, meter_id, reading_id)',
         'This meter and reading are already on the run - a reading cannot be billed '
         'twice in the same period.'),
    ]

    def is_invoiceable(self):
        """A line only reaches an invoice if it rests on a billable reading."""
        self.ensure_one()
        return bool(self.reading_id) and bool(self.reading_id._billable())
