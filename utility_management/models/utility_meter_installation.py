# -*- coding: utf-8 -*-
"""Meter installation history (SRS 7.2).

The SRS is explicit: when a meter is replaced the system must preserve the old meter, its
final reading, the new meter, its initial reading, the date, the reason, the technician and
the supporting photos. A status field on the meter cannot do that - it holds one value and
forgets the last one.

So the link between a meter and a service account is a RECORD with a start and an end.
The meter's own account_id is just a cache of the currently open installation; this table
is the history, and it is what a dispute, an audit or a consumption continuity check reads.
"""

from odoo import models, fields, api, _
from odoo.exceptions import ValidationError

INSTALL_REASONS = [
    ('installed', 'Installed'),
    ('tested', 'Tested'),
    ('repaired', 'Repaired'),
    ('replaced', 'Replaced'),
    ('removed', 'Removed'),
    ('faulty', 'Faulty'),
    ('retired', 'Retired'),
]


class UtilityMeterInstallation(models.Model):
    _name = 'utility.meter.installation'
    _description = 'Utility Meter Installation'
    _inherit = ['mail.thread']
    _order = 'date_installed desc, id desc'

    meter_id = fields.Many2one('utility.meter', string='Meter', required=True,
                               ondelete='restrict', index=True, tracking=True)
    account_id = fields.Many2one('utility.service.account', string='Service Account',
                                 required=True, ondelete='restrict', index=True,
                                 tracking=True)
    partner_id = fields.Many2one(related='account_id.partner_id', store=True, readonly=True)

    date_installed = fields.Date('Installed On', required=True,
                                 default=fields.Date.context_today, tracking=True)
    date_removed = fields.Date('Removed On', tracking=True,
                               help='Empty means this meter is still on the account.')
    initial_reading = fields.Float('Initial Reading', tracking=True,
                                   help='Dial value when the meter went on site. A new '
                                        'meter normally starts at zero.')
    final_reading = fields.Float('Final Reading', tracking=True,
                                 help='Dial value when the meter came off. Together with '
                                      'the initial reading of the replacement this is what '
                                      'keeps consumption continuous across a swap.')
    reason = fields.Selection(INSTALL_REASONS, default='installed', required=True,
                              tracking=True)
    technician_id = fields.Many2one('res.users', string='Technician',
                                    default=lambda s: s.env.user, tracking=True)
    image = fields.Image('Photo', help='Evidence photo of the meter at install or removal.')
    note = fields.Text('Notes')
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(related='account_id.company_id', store=True, readonly=True)

    is_current = fields.Boolean('Currently Installed', compute='_compute_is_current',
                                store=True)

    @api.depends('date_removed')
    def _compute_is_current(self):
        for rec in self:
            rec.is_current = not rec.date_removed

    @api.constrains('date_installed', 'date_removed')
    def _check_dates(self):
        for rec in self:
            if rec.date_removed and rec.date_removed < rec.date_installed:
                raise ValidationError(_(
                    "Meter %s cannot be removed before it was installed.", rec.meter_id.name))

    @api.constrains('meter_id', 'date_removed')
    def _check_single_open_installation(self):
        """A meter can only be on one account at a time - the whole point of the history."""
        for rec in self.filtered(lambda r: not r.date_removed):
            other = self.search_count([
                ('meter_id', '=', rec.meter_id.id),
                ('date_removed', '=', False),
                ('id', '!=', rec.id),
            ])
            if other:
                raise ValidationError(_(
                    "Meter %s is already installed on another service account. Remove it "
                    "there first, or use Replace Meter.", rec.meter_id.name))

    @api.constrains('meter_id', 'account_id')
    def _check_utility_match(self):
        for rec in self:
            if rec.meter_id.utility_type != rec.account_id.utility_type:
                raise ValidationError(_(
                    "Meter %(m)s measures %(mt)s but account %(a)s supplies %(at)s.",
                    m=rec.meter_id.name, mt=rec.meter_id.utility_type,
                    a=rec.account_id.name, at=rec.account_id.utility_type))

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        # Keep the meter's cached pointer in step with the open installation.
        for rec in records.filtered(lambda r: not r.date_removed):
            rec.meter_id.sudo().write({'account_id': rec.account_id.id})
        return records

    def write(self, vals):
        res = super().write(vals)
        if 'date_removed' in vals:
            for rec in self:
                if rec.date_removed and rec.meter_id.account_id == rec.account_id:
                    rec.meter_id.sudo().write({'account_id': False})
                elif not rec.date_removed:
                    rec.meter_id.sudo().write({'account_id': rec.account_id.id})
        return res
