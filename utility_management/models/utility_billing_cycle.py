# -*- coding: utf-8 -*-
"""Billing cycles (SRS 6, 34).

A cycle answers two questions for a service account: which period are we billing, and
when is the resulting invoice due. Keeping it as a record rather than a selection on the
meter means a utility can run "Monthly, read on the 25th, due 14 days later" alongside
"Quarterly, due on receipt" without a code change, and the billing run can name the
cycle it processed.
"""

from dateutil.relativedelta import relativedelta

from odoo import models, fields, api, _
from odoo.exceptions import ValidationError

# Period length per frequency. Used for both "when is the next period" and "is this
# account due", so the two can never disagree.
FREQUENCY_DELTA = {
    'monthly': relativedelta(months=1),
    'quarterly': relativedelta(months=3),
    'yearly': relativedelta(years=1),
}


class UtilityBillingCycle(models.Model):
    _name = 'utility.billing.cycle'
    _description = 'Utility Billing Cycle'
    _order = 'sequence, id'

    name = fields.Char('Cycle', required=True)
    sequence = fields.Integer(default=10)
    frequency = fields.Selection([
        ('monthly', 'Monthly'),
        ('quarterly', 'Quarterly'),
        ('yearly', 'Yearly'),
    ], default='monthly', required=True)
    anchor_day = fields.Integer(
        'Period Starts On Day', default=1,
        help='Day of the month a period opens. 1 means calendar months. A cycle anchored '
             'on 25 runs 25 Jan - 24 Feb, which is how a utility that reads mid-month '
             'bills.')
    due_days = fields.Integer(
        'Payment Due (days)', default=14,
        help='Days after the invoice date the bill falls due. 0 means due on receipt.')
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company)

    @api.constrains('anchor_day')
    def _check_anchor_day(self):
        for cycle in self:
            # 28 is the last day present in every month; beyond it a period would skip
            # February entirely in a common year.
            if not 1 <= cycle.anchor_day <= 28:
                raise ValidationError(_(
                    "Period start day must be between 1 and 28 - a later day does not "
                    "exist in every month."))

    @api.constrains('due_days')
    def _check_due_days(self):
        for cycle in self:
            if cycle.due_days < 0:
                raise ValidationError(_("Payment due days cannot be negative."))

    def period_containing(self, date):
        """Return (start, end_inclusive) of the period this date falls in."""
        self.ensure_one()
        anchor = self.anchor_day or 1
        start = date.replace(day=anchor) if date.day >= anchor else \
            (date.replace(day=anchor) - FREQUENCY_DELTA[self.frequency])
        # Quarterly/yearly cycles step in whole periods from that anchored month.
        return start, start + FREQUENCY_DELTA[self.frequency] - relativedelta(days=1)

    def previous_period(self, start):
        """The period immediately before the one opening on `start`."""
        self.ensure_one()
        prev_start = start - FREQUENCY_DELTA[self.frequency]
        return prev_start, start - relativedelta(days=1)

    def is_due(self, last_billed_date, on_date=None):
        """True when a full period has elapsed since the last bill (or never billed)."""
        self.ensure_one()
        if not last_billed_date:
            return True
        on_date = on_date or fields.Date.today()
        return last_billed_date + FREQUENCY_DELTA[self.frequency] <= on_date

    def due_date_for(self, invoice_date):
        self.ensure_one()
        return invoice_date + relativedelta(days=self.due_days or 0)
