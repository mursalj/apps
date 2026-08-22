# -*- coding: utf-8 -*-
"""Meter replacement (SRS 7.2).

Swapping a meter is the one operation that can silently destroy a customer's consumption
history: the new dial starts near zero, so a naive next reading looks like negative usage
and either blocks or bills nonsense. The SRS therefore requires the old meter, its FINAL
reading, the new meter, its INITIAL reading, the date, reason, technician and evidence to
be captured together.

This wizard does all of it in one transaction: closes the old installation, opens the new
one, and (by default) writes the two readings that keep consumption continuous — a
replacement reading closing the old meter, and an opening reading starting the new one.
"""

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

from odoo.addons.utility_management.models.utility_meter_installation import INSTALL_REASONS


class UtilityMeterReplaceWizard(models.TransientModel):
    _name = 'utility.meter.replace.wizard'
    _description = 'Replace Meter'

    old_meter_id = fields.Many2one('utility.meter', string='Meter Being Removed',
                                   required=True)
    account_id = fields.Many2one('utility.service.account', string='Service Account',
                                 related='old_meter_id.account_id', readonly=True)
    utility_type = fields.Selection(related='old_meter_id.utility_type', readonly=True)
    last_reading = fields.Float(related='old_meter_id.last_reading', readonly=True,
                                string='Last Recorded Reading')

    final_reading = fields.Float('Final Reading (old meter)', required=True,
                                 help='Dial value read off the meter as it came out.')
    new_meter_id = fields.Many2one(
        'utility.meter', string='Replacement Meter', required=True,
        domain="[('account_id', '=', False), ('utility_type', '=', utility_type),"
               " ('state', 'in', ('draft', 'active'))]")
    initial_reading = fields.Float('Initial Reading (new meter)', default=0.0,
                                   help='Dial value on the replacement. Usually zero.')

    date = fields.Date('Replacement Date', required=True,
                       default=fields.Date.context_today)
    reason = fields.Selection(INSTALL_REASONS, default='replaced', required=True)
    technician_id = fields.Many2one('res.users', string='Technician',
                                    default=lambda s: s.env.user)
    image = fields.Image('Evidence Photo')
    note = fields.Text('Notes')
    create_readings = fields.Boolean(
        'Record Closing / Opening Readings', default=True,
        help='Writes a replacement reading on the old meter and an opening reading on the '
             'new one, so the final slice of consumption is billable and the new meter '
             'starts from a known point. Clear only if you will enter them by hand.')

    @api.constrains('final_reading')
    def _check_final_reading(self):
        for wiz in self:
            if wiz.final_reading < wiz.last_reading:
                raise ValidationError(_(
                    "The final reading (%(f)s) is below the last recorded reading (%(l)s) "
                    "on meter %(m)s. A dial does not run backwards — check the figure, or "
                    "record the rollover on the meter first.",
                    f=wiz.final_reading, l=wiz.last_reading, m=wiz.old_meter_id.name))

    def action_replace(self):
        self.ensure_one()
        old, new = self.old_meter_id, self.new_meter_id
        account = self.account_id
        if not account:
            raise UserError(_(
                "Meter %s is not installed on a service account, so there is nothing to "
                "replace. Assign it first.", old.name))
        if new == old:
            raise UserError(_("The replacement meter must be a different meter."))

        Installation = self.env['utility.meter.installation']
        open_installation = Installation.search([
            ('meter_id', '=', old.id), ('date_removed', '=', False)], limit=1)
        if not open_installation:
            # Data predating the history table, or a hand-edited record: rather than
            # refuse, write the row that should have existed so history stays complete.
            open_installation = Installation.create({
                'meter_id': old.id,
                'account_id': account.id,
                'date_installed': old.installation_date or account.connection_date
                or self.date,
                'initial_reading': old.initial_reading,
                'reason': 'installed',
                'technician_id': self.technician_id.id,
            })

        open_installation.write({
            'date_removed': self.date,
            'final_reading': self.final_reading,
            'reason': self.reason,
            'technician_id': self.technician_id.id,
            'image': self.image,
            'note': self.note,
        })
        old.write({'state': 'faulty' if self.reason == 'faulty' else 'decommissioned'})

        Installation.create({
            'meter_id': new.id,
            'account_id': account.id,
            'date_installed': self.date,
            'initial_reading': self.initial_reading,
            'reason': 'installed',
            'technician_id': self.technician_id.id,
            'image': self.image,
            'note': self.note,
        })
        new.write({
            'state': 'active',
            'installation_date': new.installation_date or self.date,
            'initial_reading': self.initial_reading,
        })

        readings = self.env['utility.meter.reading']
        if self.create_readings:
            when = fields.Datetime.to_datetime(self.date)
            closing = readings.create({
                'meter_id': old.id,
                'reading_date': when,
                'reading_type': 'replacement',
                'present_reading': self.final_reading,
                'reader_id': self.technician_id.id,
            })
            opening = readings.create({
                'meter_id': new.id,
                'reading_date': when,
                'reading_type': 'opening',
                'present_reading': self.initial_reading,
                'reader_id': self.technician_id.id,
            })
            # Validated, not draft: these are witnessed figures, and the closing one has to
            # be billable for the customer's last slice of usage to reach an invoice.
            (closing | opening).action_validate()
            readings = closing | opening

        account.message_post(body=_(
            "Meter %(old)s replaced by %(new)s on %(date)s (final %(f)s, initial %(i)s).",
            old=old.name, new=new.name, date=self.date,
            f=self.final_reading, i=self.initial_reading))

        return {
            'type': 'ir.actions.act_window',
            'name': _('Meter History — %s', account.name),
            'res_model': 'utility.meter.installation',
            'view_mode': 'list,form',
            'domain': [('account_id', '=', account.id)],
        }
