# -*- coding: utf-8 -*-
"""Meter registry (SRS 7.1).

A meter is the physical device. It measures one utility, has a serial, a dial width and a
CT multiplier, and it is installed on a SERVICE ACCOUNT for a period of time - never
directly on a customer. That indirection is what lets a meter be swapped, moved or held in
stock without losing the account's consumption history (see utility.meter.installation).

Meter state is PHYSICAL (working, in maintenance, faulty, retired). The commercial state
of the connection - active, suspended, disconnected - belongs to the service account.
"""

from odoo import models, fields, api, _
from odoo.exceptions import ValidationError

from .utility_tariff import UTILITY_TYPES


class UtilityMeter(models.Model):
    _name = 'utility.meter'
    _description = 'Utility Meter'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'name'

    name = fields.Char('Meter No.', required=True, copy=False, index=True, tracking=True,
                       help='Serial / barcode - the unique meter identifier.')
    utility_type = fields.Selection(UTILITY_TYPES, string='Utility', required=True,
                                    tracking=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('maintenance', 'Maintenance'),
        ('faulty', 'Faulty'),
        ('decommissioned', 'Decommissioned'),
    ], default='draft', required=True, tracking=True)

    # --- assignment ---
    account_id = fields.Many2one('utility.service.account', string='Service Account',
                                 ondelete='restrict', index=True, tracking=True,
                                 help='Account this meter currently serves. Maintained '
                                      'from the open installation record.')
    partner_id = fields.Many2one('res.partner', string='Customer',
                                 related='account_id.partner_id', store=True, readonly=True)
    installation_ids = fields.One2many('utility.meter.installation', 'meter_id',
                                       string='Installation History')
    tariff_id = fields.Many2one('utility.tariff', string='Tariff Override', tracking=True,
                                help='Leave empty to bill on the account tariff. Set only '
                                     'when this meter is charged differently.')
    effective_tariff_id = fields.Many2one('utility.tariff', string='Effective Tariff',
                                          compute='_compute_effective_tariff', store=True,
                                          help='The tariff billing actually uses: the '
                                               'meter override if set, else the account.')

    multiplier = fields.Float('Multiplier', default=1.0, tracking=True,
                              help='CT ratio: raw dial movement is multiplied by this to '
                                   'get real consumption. 1 for a direct meter.')
    digits = fields.Integer('Dial Digits', default=6, tracking=True,
                            help='Number of digits on the dial. Needed to detect a '
                                 'rollover: a 6-digit meter wraps from 999999 to 0.')

    # --- device detail (SRS 7.1) ---
    meter_type = fields.Selection([
        ('mechanical', 'Mechanical'),
        ('digital', 'Digital'),
        ('smart', 'Smart'),
        ('prepaid', 'Prepaid'),
    ], string='Meter Type', default='mechanical', tracking=True)
    manufacturer = fields.Char('Manufacturer')
    model_name = fields.Char('Model')
    communication_type = fields.Selection([
        ('manual', 'Manual Reading'),
        ('amr', 'AMR - Walk/Drive-by'),
        ('ami', 'AMI - Remote / Smart'),
    ], string='Communication', default='manual',
        help='How readings reach the system. Drives which meters a field route includes.')
    installation_date = fields.Date('Installation Date', tracking=True)
    initial_reading = fields.Float('Initial Reading',
                                   help='Dial value when this meter was first commissioned.')
    calibration_date = fields.Date('Last Calibration')
    last_inspection = fields.Date('Last Inspection')
    next_inspection = fields.Date('Next Inspection')
    image = fields.Image('Meter Photo')

    # --- location (falls back to the account's service address) ---
    street = fields.Char('Address')
    city = fields.Char('City')
    latitude = fields.Float('Latitude', digits=(10, 7))
    longitude = fields.Float('Longitude', digits=(10, 7))

    contract_start = fields.Date('Contract Start')
    contract_end = fields.Date('Contract End')

    # --- readings ---
    reading_ids = fields.One2many('utility.meter.reading', 'meter_id', string='Readings')
    reading_count = fields.Integer(compute='_compute_reading_stats')
    last_reading = fields.Float(compute='_compute_reading_stats',
                                help='Most recent present reading recorded.')
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company)

    # with the same serial, which happens whenever they buy from the same supplier.
    #
    # The SQL constraint cannot do this alone. Postgres treats NULLs as distinct in a
    # or anything created before allocation) would both be accepted with the same serial.
    # _check_unique_serial closes that hole; the constraint stays because a database-level
    # guarantee is still worth having for the stamped rows.
    _sql_constraints = [
        ('name_company_uniq', 'unique(name, company_id)',
         'A meter with this number already exists.'),
    ]

    @api.constrains('name', 'company_id')
    def _check_unique_serial(self):
        for meter in self:
            duplicate = self.sudo().search_count([
                ('id', '!=', meter.id),
                ('name', '=', meter.name),
                ('company_id', '=', meter.company_id.id),
            ])
            if duplicate:
                raise ValidationError(_(
                    "Meter %s already exists. A serial number identifies one physical "
                    "device - reusing it would merge two meters' histories.", meter.name))

    @api.depends('tariff_id', 'account_id.tariff_id')
    def _compute_effective_tariff(self):
        for meter in self:
            meter.effective_tariff_id = meter.tariff_id or meter.account_id.tariff_id

    @api.depends('reading_ids.present_reading', 'reading_ids.state')
    def _compute_reading_stats(self):
        counts = {m.id: c for m, c in self.env['utility.meter.reading']._read_group(
            [('meter_id', 'in', self.ids), ('state', '!=', 'cancel')],
            ['meter_id'], ['__count'])}
        for meter in self:
            key = meter._origin.id or meter.id
            meter.reading_count = counts.get(key, 0)
            valid = meter.reading_ids.filtered(lambda r: r.state != 'cancel')
            meter.last_reading = valid.sorted('reading_date')[-1:].present_reading or 0.0

    @api.constrains('multiplier')
    def _check_multiplier(self):
        for meter in self:
            if meter.multiplier <= 0:
                raise ValidationError(_("Multiplier must be greater than zero."))

    @api.constrains('digits')
    def _check_digits(self):
        for meter in self:
            if meter.digits and not 1 <= meter.digits <= 12:
                raise ValidationError(_(
                    "Dial digits must be between 1 and 12 - %s is not a real meter.",
                    meter.digits))

    @api.constrains('tariff_id', 'utility_type')
    def _check_tariff_type(self):
        for meter in self:
            if meter.tariff_id and meter.tariff_id.utility_type != meter.utility_type:
                raise ValidationError(_(
                    "Tariff '%(t)s' is for %(tt)s but meter %(m)s measures %(mt)s.",
                    t=meter.tariff_id.name, tt=meter.tariff_id.utility_type,
                    m=meter.name, mt=meter.utility_type))

    @api.constrains('account_id', 'utility_type')
    def _check_account_type(self):
        for meter in self:
            if meter.account_id and meter.account_id.utility_type != meter.utility_type:
                raise ValidationError(_(
                    "Meter %(m)s measures %(mt)s and cannot serve account %(a)s (%(at)s).",
                    m=meter.name, mt=meter.utility_type,
                    a=meter.account_id.name, at=meter.account_id.utility_type))

    def action_activate(self):
        self.write({'state': 'active'})

    def action_maintenance(self):
        self.write({'state': 'maintenance'})

    def action_mark_faulty(self):
        self.write({'state': 'faulty'})

    def action_decommission(self):
        self.write({'state': 'decommissioned'})

    def action_view_readings(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Readings - %s', self.name),
            'res_model': 'utility.meter.reading',
            'view_mode': 'list,form',
            'domain': [('meter_id', '=', self.id)],
            'context': {'default_meter_id': self.id},
        }

    def action_replace(self):
        """Open the replacement wizard for this meter (SRS 7.2)."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Replace Meter %s', self.name),
            'res_model': 'utility.meter.replace.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_old_meter_id': self.id},
        }

    def _billing_product(self):
        """Service product carrying the income account + taxes for the invoice line.

        Order of preference: the tariff's explicit product, else a per-utility service
        product resolved (created on first use) by default_code. Products are a shared
        config concept here, so a single service product per utility type serves every
        """
        self.ensure_one()
        tariff = self.effective_tariff_id
        if tariff.product_id:
            return tariff.product_id
        code = 'UTIL-%s' % self.utility_type.upper()
        product = self.env['product.product'].sudo().search(
            [('default_code', '=', code)], limit=1)
        if not product:
            product = self.env['product.product'].sudo().create({
                'name': _('%s Service') % dict(self._fields['utility_type'].selection)[
                    self.utility_type],
                'default_code': code,
                'type': 'service',
                'sale_ok': True,
                'purchase_ok': False,
                'invoice_policy': 'order',
            })
        return product
