# -*- coding: utf-8 -*-
"""Reading ingestion, consumption, validation and estimation (SRS 8, 9, 10, 14).

A reading records the meter's dial value. Consumption is the movement since the previous
reading, scaled by the meter multiplier:

    consumption = (present - previous) x multiplier

Three things this module refuses to do, all of them deliberate:

* **It does not reject a reading.** A dial that reads lower than last month is a fact,
  whatever caused it — a rollover, a swap, a mis-key, or tampering. Refusing to store it
  (which is what this model used to do) means the field worker records nothing at all and
  the truth is lost. Every anomaly is captured, classified and queued for review instead
  (SRS 9), and only a clean or approved reading reaches a bill.
* **It does not silently bill a negative.** A negative movement after a BILLED estimate is
  an over-estimate that has to come back to the customer as a credit — see
  _trueup_state and _prepare_invoice_lines.
* **It does not run a query per record.** Both the previous-reading lookup and the
  baseline average are resolved for the whole recordset in one pass; a billing run over
  100k meters cannot afford anything else.
"""

from collections import defaultdict
from datetime import timedelta

from odoo import models, fields, api, _
from odoo.exceptions import ValidationError, UserError

from .utility_service_account import BILLABLE_STATES

# Consumption above this multiple of the trailing average is flagged high-usage.
DEFAULT_HIGH_USAGE_FACTOR = 1.5
# ...and below this fraction of it, low-usage. Both are the SRS 9 "abnormal" checks.
DEFAULT_LOW_USAGE_FACTOR = 0.5
# A wrapped dial is only believable if the resulting usage is within this multiple of the
# meter's own history. Beyond it, the reading is far more likely to be a mis-key.
DEFAULT_ROLLOVER_TOLERANCE = 3.0
# How many prior readings form the "recent average" baseline.
BASELINE_WINDOW = 3

# Reading types whose dial starts from zero rather than continuing the previous one.
FRESH_DIAL_TYPES = ('swap', 'opening')

# Exception states an unreviewed reading can carry. 'normal' and 'estimated' are billable;
# the rest need a human before any money is calculated from them.
BILLABLE_EXCEPTIONS = ('normal', 'estimated')


class UtilityMeterReading(models.Model):
    _name = 'utility.meter.reading'
    _description = 'Utility Meter Reading'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'utility.sequence.mixin']
    _order = 'reading_date desc, id desc'

    name = fields.Char('Reference', default='New', copy=False, readonly=True, index=True)
    meter_id = fields.Many2one('utility.meter', string='Meter', required=True,
                               ondelete='restrict', index=True, tracking=True)
    account_id = fields.Many2one('utility.service.account', string='Service Account',
                                 related='meter_id.account_id', store=True, readonly=True,
                                 index=True)
    partner_id = fields.Many2one(related='meter_id.partner_id', store=True, readonly=True)
    utility_type = fields.Selection(related='meter_id.utility_type', store=True, readonly=True)
    reading_date = fields.Datetime('Reading Date', required=True,
                                   default=fields.Datetime.now, tracking=True)
    reading_type = fields.Selection([
        ('actual', 'Actual'),
        ('estimated', 'Estimated'),
        ('customer', 'Customer Submitted'),
        ('remote', 'Remote / Automatic'),
        ('opening', 'Opening Reading'),
        ('closing', 'Closing Reading'),
        ('replacement', 'Replacement Reading'),
        ('corrected', 'Corrected Reading'),
        ('swap', 'Meter Swap / Reset'),
    ], string='Type', default='actual', required=True, tracking=True)

    present_reading = fields.Float('Present Reading', required=True, tracking=True)
    previous_reading = fields.Float('Previous Reading', compute='_compute_previous',
                                    store=True)
    multiplier = fields.Float(related='meter_id.multiplier', readonly=True)
    consumption = fields.Float('Consumption', compute='_compute_consumption', store=True,
                               tracking=True)
    is_rollover = fields.Boolean('Dial Rolled Over', compute='_compute_consumption',
                                 store=True,
                                 help='The dial passed its maximum and wrapped to zero. '
                                      'Consumption is measured across the wrap.')

    # --- field capture (SRS 8, 21) ---
    image = fields.Image('Reading Photo', help='Photo of the meter dial as proof.')
    reader_id = fields.Many2one('res.users', string='Read By',
                                default=lambda s: s.env.user, tracking=True,
                                help='Who took the reading. Required for field audit and '
                                     'meter-reader productivity reporting (SRS 24).')
    condition = fields.Selection([
        ('ok', 'Normal'),
        ('damaged', 'Damaged'),
        ('unreadable', 'Unreadable'),
        ('inaccessible', 'No Access'),
        ('tampered', 'Signs of Tampering'),
    ], string='Meter Condition', default='ok', tracking=True)
    access_problem = fields.Char('Access Problem',
                                 help='Locked gate, dog, occupier absent — why the meter '
                                      'could not be read normally.')
    latitude = fields.Float('Latitude', digits=(10, 7))
    longitude = fields.Float('Longitude', digits=(10, 7))
    note = fields.Text('Notes')

    # --- validation outcome (SRS 9) ---
    exception_state = fields.Selection([
        ('normal', 'Normal'),
        ('review', 'Review Required'),
        ('estimated', 'Estimated'),
        ('invalid', 'Invalid'),
        ('tampering', 'Suspected Tampering'),
    ], string='Exception', default='normal', required=True, tracking=True, index=True)
    exception_codes = fields.Char('Exception Codes', readonly=True, copy=False,
                                  help='Machine-readable list of the checks that fired.')
    exception_reason = fields.Text('Exception Detail', readonly=True, copy=False)
    reviewed_by = fields.Many2one('res.users', string='Reviewed By', readonly=True,
                                  copy=False, tracking=True)
    reviewed_on = fields.Datetime('Reviewed On', readonly=True, copy=False)
    review_note = fields.Text('Review Note', copy=False)

    is_high_usage = fields.Boolean('High Usage', compute='_compute_anomaly', store=True)
    is_low_usage = fields.Boolean('Low Usage', compute='_compute_anomaly', store=True)
    baseline_average = fields.Float('Recent Average', compute='_compute_baseline',
                                    store=True)

    state = fields.Selection([
        ('draft', 'Draft'),
        ('validated', 'Validated'),
        ('billed', 'Billed'),
        ('cancel', 'Cancelled'),
    ], default='draft', required=True, tracking=True, index=True)

    invoice_id = fields.Many2one('account.move', string='Invoice', readonly=True,
                                 copy=False)
    # True-up bookkeeping: an estimated reading later corrected by an actual one.
    superseded = fields.Boolean('Superseded by Actual', default=False, readonly=True,
                                copy=False)
    trueup_reading_ids = fields.Many2many(
        'utility.meter.reading', 'utility_reading_trueup_rel', 'actual_id', 'estimate_id',
        string='Estimates Corrected', readonly=True, copy=False,
        help='Billed estimates this actual reading corrects. Their over- or under-charge '
             'is settled on the invoice this reading produces (SRS 14).')
    billing_run_id = fields.Many2one('utility.billing.run', string='Billing Run',
                                     readonly=True, copy=False, index=True,
                                     help='The approved run that locked this reading.')
    company_id = fields.Many2one(related='meter_id.company_id', store=True, readonly=True)

    _sql_constraints = [
        ('present_reading_positive', 'CHECK(present_reading >= 0)',
         'A meter dial cannot show a negative value.'),
    ]

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    @api.model
    def _config_float(self, key, default):
        param = self.env['ir.config_parameter'].sudo().get_param(
            'utility_management.%s' % key, default)
        try:
            return float(param)
        except (TypeError, ValueError):
            return default

    @api.model
    def _high_usage_factor(self):
        return self._config_float('high_usage_factor', DEFAULT_HIGH_USAGE_FACTOR)

    @api.model
    def _low_usage_factor(self):
        return self._config_float('low_usage_factor', DEFAULT_LOW_USAGE_FACTOR)

    @api.model
    def _rollover_tolerance(self):
        return self._config_float('rollover_tolerance', DEFAULT_ROLLOVER_TOLERANCE)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == 'New':
                vals['name'] = self._next_reference('utility.meter.reading') or 'New'
        records = super().create(vals_list)
        records._mark_successors_dirty()
        records._evaluate_exceptions()
        return records

    def write(self, vals):
        """Once billed, a reading is immutable except for review bookkeeping (SRS 47.7)."""
        locked = {'present_reading', 'previous_reading', 'consumption', 'reading_type',
                  'reading_date', 'meter_id'}
        touched = locked & set(vals)
        if touched:
            for rec in self:
                if rec.state == 'billed':
                    raise UserError(_(
                        "Reading %s has been billed and can no longer be edited. Post a "
                        "corrected reading instead.", rec.name))
                # Period lock (SRS 35): an approved run is a figure someone signed off.
                if rec.billing_run_id.state in ('approved', 'invoiced'):
                    raise UserError(_(
                        "Reading %(n)s belongs to billing run %(r)s, which has been "
                        "approved. Reopen the run, or post a corrected reading.",
                        n=rec.name, r=rec.billing_run_id.name))
        res = super().write(vals)
        if touched:
            self._mark_successors_dirty()
            self._evaluate_exceptions()
        return res

    def _mark_successors_dirty(self):
        """Recompute previous_reading on every reading that comes AFTER these.

        previous_reading is stored and depends on the meter's ordering, not on this row's
        own values, so Odoo cannot know that inserting a back-dated reading invalidates the
        ones after it. Without this, a late entry leaves every later reading quoting the
        wrong opening figure and billing the wrong consumption — silently.
        """
        # The new rows have to be in the database before anything recomputes against
        # them: the successors' previous_reading is resolved with a SELECT, and a reading
        # still sitting in the ORM's write buffer is invisible to it.
        self.env.flush_all()
        successors = self.browse()
        for meter, records in self._group_by_meter().items():
            earliest = min(records.mapped('reading_date'))
            successors |= self.sudo().search([
                ('meter_id', '=', meter),
                ('reading_date', '>=', earliest),
                ('id', 'not in', records.ids),
                ('state', '!=', 'cancel'),
            ])
        if successors:
            # modified() rather than add_to_compute(): it marks everything DOWNSTREAM of
            # the changed dependencies too, so consumption, the baseline and the anomaly
            # flags are recomputed as well. Scheduling previous_reading alone would leave
            # the later readings quoting a corrected opening figure against a stale
            # consumption.
            successors.modified(['reading_date', 'meter_id'])
            successors._evaluate_exceptions()

    def _group_by_meter(self):
        grouped = defaultdict(lambda: self.browse())
        for rec in self:
            if rec.meter_id:
                grouped[rec.meter_id.id] |= rec
        return grouped

    # ------------------------------------------------------------------
    # Consumption
    # ------------------------------------------------------------------
    @api.depends('meter_id', 'reading_date')
    def _compute_previous(self):
        """Previous = the last non-cancelled reading of the same meter before this one.

        Resolved for the whole recordset with ONE query. The old implementation ran a
        search() per record, so validating a month of readings for 10,000 meters cost
        10,000 round trips.
        """
        history = self._meter_history()
        for rec in self:
            series = history.get(rec.meter_id.id or rec.meter_id._origin.id, [])
            key = rec._sort_key()
            previous = 0.0
            for other_key, present in series:
                if other_key >= key:
                    break
                previous = present
            rec.previous_reading = previous

    def _sort_key(self):
        """Order readings by date, then id — the same order the list view shows."""
        self.ensure_one()
        return (self.reading_date or fields.Datetime.now(), self._origin.id or 0)

    def _meter_history(self):
        """{meter_id: [(sort_key, present_reading), ...]} for every meter in the set."""
        meter_ids = [m.id for m in self.mapped('meter_id')._origin if m.id]
        if not meter_ids:
            return {}
        rows = self.sudo().search_read(
            [('meter_id', 'in', meter_ids), ('state', '!=', 'cancel')],
            ['meter_id', 'reading_date', 'present_reading'],
            order='reading_date asc, id asc')
        history = defaultdict(list)
        own_ids = set(self._origin.ids)
        for row in rows:
            if row['id'] in own_ids:
                continue  # a reading is never its own predecessor
            history[row['meter_id'][0]].append(
                ((row['reading_date'], row['id']), row['present_reading']))
        return history

    @api.depends('meter_id', 'reading_date')
    def _compute_baseline(self):
        """Average consumption of the meter's last few settled readings.

        Deliberately independent of this reading's own consumption, so the rollover and
        anomaly checks can use it without a circular dependency.
        """
        meter_ids = [m.id for m in self.mapped('meter_id')._origin if m.id]
        history = defaultdict(list)
        if meter_ids:
            rows = self.sudo().search_read(
                [('meter_id', 'in', meter_ids), ('state', 'in', ('validated', 'billed')),
                 ('consumption', '>', 0)],
                ['meter_id', 'reading_date', 'consumption'],
                order='reading_date desc, id desc')
            own_ids = set(self._origin.ids)
            for row in rows:
                if row['id'] in own_ids:
                    continue
                history[row['meter_id'][0]].append((row['reading_date'], row['consumption']))
        for rec in self:
            series = history.get(rec.meter_id.id or rec.meter_id._origin.id, [])
            recent = [c for _dt, c in series[:BASELINE_WINDOW]]
            rec.baseline_average = sum(recent) / len(recent) if recent else 0.0

    @api.depends('present_reading', 'previous_reading', 'multiplier', 'reading_type',
                 'meter_id.digits', 'baseline_average')
    def _compute_consumption(self):
        """Movement since the previous reading, across a dial rollover where plausible.

        A 6-digit meter reading 999,900 last month and 000,150 this month has used 250
        units, not minus 999,750. The wrap is only accepted when the resulting figure is
        in the same league as the meter's own history (rollover_tolerance), because the
        far more common cause of a lower dial is a mis-keyed digit — and treating that as
        a rollover would bill the customer for a million units.
        """
        tolerance = self._rollover_tolerance()
        for rec in self:
            multiplier = rec.multiplier or 1.0
            rec.is_rollover = False
            if rec.reading_type in FRESH_DIAL_TYPES:
                # A fresh or replaced meter starts at zero, so its own dial value is the use.
                rec.consumption = rec.present_reading * multiplier
                continue
            raw = rec.present_reading - rec.previous_reading
            if raw >= 0:
                rec.consumption = raw * multiplier
                continue
            wrapped = rec._wrapped_movement()
            if wrapped is not None:
                plausible = rec.baseline_average <= 0 or \
                    wrapped * multiplier <= rec.baseline_average * tolerance
                if plausible:
                    rec.is_rollover = True
                    rec.consumption = wrapped * multiplier
                    continue
            # Genuinely negative: kept as-is so the exception engine can classify it and a
            # reviewer can see the real numbers. Nothing bills off this until reviewed.
            rec.consumption = raw * multiplier

    def _wrapped_movement(self):
        """Dial movement assuming a rollover, or None when the meter has no dial width."""
        self.ensure_one()
        digits = self.meter_id.digits
        if not digits:
            return None
        ceiling = 10 ** digits
        if self.previous_reading >= ceiling:
            return None
        return (ceiling - self.previous_reading) + self.present_reading

    @api.depends('consumption', 'baseline_average')
    def _compute_anomaly(self):
        high = self._high_usage_factor()
        low = self._low_usage_factor()
        for rec in self:
            average = rec.baseline_average
            rec.is_high_usage = bool(average > 0 and rec.consumption > average * high)
            rec.is_low_usage = bool(
                average > 0 and 0 <= rec.consumption < average * low)

    # ------------------------------------------------------------------
    # Validation (SRS 9)
    # ------------------------------------------------------------------
    def _evaluate_exceptions(self):
        """Classify each reading and record why. Never raises — that is the point.

        A reviewed reading is left alone: once a human has ruled on it, an unrelated edit
        elsewhere must not quietly reopen or close the case.
        """
        for rec in self:
            if rec.reviewed_by or rec.state in ('billed', 'cancel'):
                continue
            codes, reasons = rec._run_checks()
            rec.exception_codes = ','.join(codes) if codes else False
            rec.exception_reason = '\n'.join(reasons) if reasons else False
            rec.exception_state = rec._exception_state_for(codes)

    def _exception_state_for(self, codes):
        """Worst outcome wins: tampering > invalid > review > estimated > normal."""
        self.ensure_one()
        if 'tampering' in codes:
            return 'tampering'
        if 'negative' in codes:
            # Movement we cannot explain: not a rollover, not a swap, not a true-up.
            return 'invalid'
        if codes:
            return 'review'
        if self.reading_type == 'estimated':
            return 'estimated'
        return 'normal'

    def _run_checks(self):
        """Return (codes, human-readable reasons) for this reading."""
        self.ensure_one()
        codes, reasons = [], []

        if self.condition == 'tampered':
            codes.append('tampering')
            reasons.append(_("The reader reported signs of tampering on the meter."))

        if self.is_rollover:
            codes.append('rollover')
            reasons.append(_(
                "The dial rolled over: %(prev)s to %(now)s on a %(d)s-digit meter, read as "
                "%(c)s units.", prev=self.previous_reading, now=self.present_reading,
                d=self.meter_id.digits, c=round(self.consumption, 2)))
        elif self.consumption < 0:
            if self._trueup_state():
                codes.append('trueup')
                reasons.append(_(
                    "Lower than the estimate that was already billed. The over-charge is "
                    "credited back on the next invoice."))
            else:
                codes.append('negative')
                reasons.append(_(
                    "Reading %(now)s is below the previous reading %(prev)s. If the meter "
                    "was reset or replaced, set the type accordingly; if the dial wrapped, "
                    "check the meter's dial digits.",
                    now=self.present_reading, prev=self.previous_reading))

        if self.consumption == 0 and self.reading_type not in FRESH_DIAL_TYPES:
            codes.append('zero')
            reasons.append(_("No consumption at all since the previous reading."))
        elif self.is_high_usage:
            codes.append('high')
            reasons.append(_(
                "Consumption %(c)s is more than %(f)sx the recent average of %(a)s.",
                c=round(self.consumption, 2), f=self._high_usage_factor(),
                a=round(self.baseline_average, 2)))
        elif self.is_low_usage:
            codes.append('low')
            reasons.append(_(
                "Consumption %(c)s is under %(f)sx the recent average of %(a)s.",
                c=round(self.consumption, 2), f=self._low_usage_factor(),
                a=round(self.baseline_average, 2)))

        if self._is_duplicate():
            codes.append('duplicate')
            reasons.append(_("Another reading already exists for this meter on this date."))

        if self.reading_date and self.reading_date > fields.Datetime.now():
            codes.append('future')
            reasons.append(_("The reading is dated in the future."))

        if self.condition in ('damaged', 'unreadable', 'inaccessible'):
            codes.append('condition')
            reasons.append(_("Meter condition reported as %s.",
                             dict(self._fields['condition'].selection)[self.condition]))

        return codes, reasons

    def _is_duplicate(self):
        self.ensure_one()
        if not self.reading_date or not self.meter_id:
            return False
        day_start = fields.Datetime.to_datetime(fields.Date.to_date(self.reading_date))
        return bool(self.sudo().search_count([
            ('meter_id', '=', self.meter_id._origin.id or self.meter_id.id),
            ('id', '!=', self._origin.id or 0),
            ('state', '!=', 'cancel'),
            ('reading_date', '>=', day_start),
            ('reading_date', '<', day_start + timedelta(days=1)),
        ]))

    def _trueup_state(self):
        """The billed estimates this reading corrects, if it is correcting any.

        Over-estimation is the one case where a lower dial than last time is not an error:
        the previous figure was never read off the meter, it was calculated, and the
        customer has already paid for it.
        """
        self.ensure_one()
        if self.reading_type not in ('actual', 'corrected', 'remote', 'customer'):
            return self.browse()
        return self.sudo().search([
            ('meter_id', '=', self.meter_id._origin.id or self.meter_id.id),
            ('reading_type', '=', 'estimated'),
            ('state', '=', 'billed'),
            ('reading_date', '<', self.reading_date),
            ('id', '!=', self._origin.id or 0),
        ], order='reading_date desc', limit=BASELINE_WINDOW)

    # ------------------------------------------------------------------
    # Review workflow (SRS 9, 35)
    # ------------------------------------------------------------------
    def _assert_may_review(self):
        if not self.env.user.has_group('utility_management.group_utility_billing'):
            raise UserError(_(
                "Only a Billing Specialist or Utility Manager may rule on a reading "
                "exception — this is a billing control, not a field decision."))

    def action_approve_exception(self):
        """Accept the reading as it stands; it becomes billable."""
        self._assert_may_review()
        for rec in self:
            if rec.exception_state == 'normal':
                continue
            rec.write({
                'exception_state': 'estimated' if rec.reading_type == 'estimated'
                else 'normal',
                'reviewed_by': self.env.user.id,
                'reviewed_on': fields.Datetime.now(),
            })
            rec.message_post(body=_(
                "Exception approved (%(codes)s). %(note)s",
                codes=rec.exception_codes or '-', note=rec.review_note or ''))

    def action_mark_invalid(self):
        """Reject the reading. It stays on file but can never reach an invoice."""
        self._assert_may_review()
        for rec in self:
            if rec.state == 'billed':
                raise UserError(_(
                    "Reading %s is already billed; reverse the invoice instead.", rec.name))
            rec.write({
                'exception_state': 'invalid',
                'reviewed_by': self.env.user.id,
                'reviewed_on': fields.Datetime.now(),
                'state': 'draft',
            })
            rec.message_post(body=_("Reading marked invalid. %s", rec.review_note or ''))

    def action_mark_tampering(self):
        self._assert_may_review()
        for rec in self:
            rec.write({
                'exception_state': 'tampering',
                'reviewed_by': self.env.user.id,
                'reviewed_on': fields.Datetime.now(),
            })
            rec.activity_schedule(
                'mail.mail_activity_data_todo',
                summary=_('Investigate suspected meter tampering'),
                note=rec.exception_reason or '',
                user_id=self.env.uid)

    def action_reopen_review(self):
        """Undo a review decision so the checks run again."""
        self._assert_may_review()
        self.write({'reviewed_by': False, 'reviewed_on': False})
        self._evaluate_exceptions()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    def action_validate(self):
        for rec in self:
            if rec.state != 'draft':
                continue
            if rec.exception_state in ('invalid', 'tampering'):
                raise UserError(_(
                    "Reading %(n)s is marked %(s)s and cannot be validated until a "
                    "Billing Specialist has ruled on it.",
                    n=rec.name, s=rec.exception_state))
            rec.state = 'validated'
            if rec.exception_state == 'review':
                rec.message_post(body=_(
                    "Validated with an open exception (%(codes)s): %(why)s",
                    codes=rec.exception_codes, why=rec.exception_reason))
            # True-up: a confirmed actual supersedes earlier unbilled estimates, and
            # records the billed ones it has to settle.
            if rec.reading_type in ('actual', 'corrected', 'remote', 'customer'):
                estimates = self.sudo().search([
                    ('meter_id', '=', rec.meter_id.id),
                    ('reading_type', '=', 'estimated'),
                    ('state', 'in', ('draft', 'validated')),
                    ('reading_date', '<', rec.reading_date),
                    ('superseded', '=', False),
                ])
                estimates.write({'superseded': True})
                billed_estimates = rec._trueup_state()
                if billed_estimates:
                    rec.trueup_reading_ids = [(6, 0, billed_estimates.ids)]

    def action_cancel(self):
        for rec in self:
            if rec.state == 'billed':
                raise UserError(_("A billed reading cannot be cancelled."))
        self.write({'state': 'cancel'})
        self._mark_successors_dirty()

    def action_reset_draft(self):
        for rec in self:
            if rec.state == 'billed':
                raise UserError(_("A billed reading cannot be reset."))
        self.write({'state': 'draft'})

    # ------------------------------------------------------------------
    # Estimation (SRS 14)
    # ------------------------------------------------------------------
    @api.model
    def _create_estimated_reading(self, meter, reading_date, consumption=None):
        """Write an estimated reading for a meter that could not be read.

        The estimated CONSUMPTION is converted back into a dial value, so the meter's
        series stays continuous: when the real reading finally arrives, its movement is
        measured against this figure and any over- or under-estimate settles itself.
        """
        account = meter.account_id
        if consumption is None:
            consumption = account._estimate_consumption(meter)
        multiplier = meter.multiplier or 1.0
        previous = meter.last_reading
        return self.create({
            'meter_id': meter.id,
            'reading_date': reading_date,
            'reading_type': 'estimated',
            'present_reading': previous + (consumption / multiplier if multiplier else 0.0),
            'note': _('Estimated by the %s method — no actual reading available.',
                      account.estimation_method or 'average'),
        })

    # ------------------------------------------------------------------
    # Billing (SRS 12, 13)
    # ------------------------------------------------------------------
    def _billable(self):
        """Readings a billing run may charge for."""
        return self.filtered(
            lambda r: r.state == 'validated' and not r.superseded
            and r.exception_state in BILLABLE_EXCEPTIONS
            and r.meter_id.account_id
            and r.meter_id.effective_tariff_id
            and r.meter_id.account_id.state in BILLABLE_STATES)

    def _prepare_invoice_lines(self, apply_penalties=False, overdue_amount=0.0):
        """Return account.move line commands for this reading's consumption.

        The arithmetic all lives in utility.tariff.compute_charges (SRS 13) — usage
        blocks, the fixed base charge, fees, minimum/maximum adjustment, discounts,
        subsidies and, when the caller has checked the account's balance, penalties. This
        method only labels the result and turns it into ORM commands, so the bill a
        customer receives and the figures a billing run previews can never diverge.

        A negative consumption reaching this point is a true-up against a billed estimate:
        it produces a negative line, which is the credit the customer is owed.
        """
        self.ensure_one()
        meter = self.meter_id
        tariff = meter.effective_tariff_id
        if not tariff:
            raise UserError(_("Meter %s has no tariff, so it cannot be billed.", meter.name))
        product = meter._billing_product()
        tax_cmd = [(6, 0, tariff.tax_ids.ids)]
        meter_detail = _('Meter %(m)s (%(start)s to %(end)s %(unit)s)',
                         m=meter.name, start=self.previous_reading,
                         end=self.present_reading, unit=tariff.unit_name)

        if self.consumption < 0:
            return [(0, 0, {
                'product_id': product.id,
                'name': _('%(name)s — over-estimate credit (%(m)s)',
                          name=tariff.name, m=meter.name),
                'quantity': self.consumption,  # negative: reduces the bill
                'price_unit': tariff.flat_rate or (
                    tariff.block_ids[:1].rate if tariff.block_ids else 0.0),
                'tax_ids': tax_cmd,
            })]

        commands = []
        for line in tariff.compute_charges(self.consumption,
                                           apply_penalties=apply_penalties,
                                           overdue_amount=overdue_amount):
            # Usage lines name the meter and its dial movement; a flat fee or a discount
            # applies to the account, not to a dial, so repeating the meter would be noise.
            label = ('%s — %s' % (line['label'], meter_detail)
                     if line['kind'] in ('usage', 'base') else line['label'])
            commands.append((0, 0, {
                'product_id': product.id,
                'name': label,
                'quantity': line['quantity'],
                'price_unit': line['price_unit'],
                'tax_ids': tax_cmd,
            }))
        return commands

    def _bill(self):
        """Group the recordset by SERVICE ACCOUNT and raise one draft invoice each.

        One bill per account is what the SRS asks for (5.2): a customer with electricity
        and water gets two bills, each with its own period, readings and balance, not one
        merged document. Readings that are not billable — unreviewed exceptions, superseded
        estimates, suspended-for-good accounts — are skipped rather than aborting the run.

        The invoice is dated at the END of the service period, not today (a run executed on
        the 3rd for December must be a December bill), and the due date comes from the
        account's billing cycle.
        """
        Move = self.env['account.move']
        billable = self._billable()
        groups = defaultdict(lambda: self.browse())
        for reading in billable:
            groups[(reading.meter_id.account_id.id, reading.company_id.id)] |= reading

        invoices = Move
        for (account_id, company_id), readings in groups.items():
            account = self.env['utility.service.account'].browse(account_id)
            lines = []
            apply_penalties = self.env.context.get('utility_apply_penalties', False)
            for reading in readings:
                lines += reading._prepare_invoice_lines(
                    apply_penalties=apply_penalties, overdue_amount=account.balance)
            if not lines:
                continue
            dates = [fields.Date.to_date(d) for d in readings.mapped('reading_date')]
            period_start, period_end = min(dates), max(dates)
            vals = {
                'move_type': 'out_invoice',
                'partner_id': account.partner_id.id,
                'company_id': company_id,
                'invoice_date': period_end,
                'invoice_origin': ', '.join(readings.mapped('name')),
                'invoice_line_ids': lines,
                'utility_account_id': account.id,
                'utility_period_start': period_start,
                'utility_period_end': period_end,
                'utility_is_estimated': any(
                    r.reading_type == 'estimated' for r in readings),
            }
            if account.billing_cycle_id:
                vals['invoice_date_due'] = account.billing_cycle_id.due_date_for(period_end)
            move = Move.with_company(company_id).create(vals)
            readings.write({'state': 'billed', 'invoice_id': move.id})
            # Settled estimates are marked so a later run cannot credit them twice.
            readings.mapped('trueup_reading_ids').write({'superseded': True})
            account.sudo().write({'last_billed_date': period_end})
            invoices |= move
        return invoices

    def action_create_invoice(self):
        """Bill the selected readings and open the resulting invoice(s)."""
        invoices = self._bill()
        if not invoices:
            raise UserError(_(
                "Nothing to bill in the selection. Readings must be Validated, free of "
                "open exceptions, on an active account with a tariff, and not already "
                "billed."))
        action = {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'context': {'default_move_type': 'out_invoice'},
        }
        if len(invoices) == 1:
            action.update(view_mode='form', res_id=invoices.id)
        else:
            action.update(view_mode='list,form',
                          domain=[('id', 'in', invoices.ids)], name=_('Utility Invoices'))
        return action

    @api.model
    def _cron_generate_bills(self):
        """Auto-bill service accounts whose billing cycle is due.

        Due-ness is a property of the ACCOUNT's cycle (last_billed_date + frequency), not
        of the meter — a customer with three meters on one account gets one bill, on one
        schedule. Drafts only; nothing is posted or sent automatically.
        """
        Account = self.env['utility.service.account'].sudo()
        due_readings = self.browse()
        for account in Account.search([('state', 'in', BILLABLE_STATES)]):
            cycle = account.billing_cycle_id
            if cycle and not cycle.is_due(account.last_billed_date):
                continue
            due_readings |= self.sudo().search([
                ('account_id', '=', account.id),
                ('state', '=', 'validated'),
                ('superseded', '=', False),
            ])
        invoices = due_readings._bill()
        return len(invoices)
