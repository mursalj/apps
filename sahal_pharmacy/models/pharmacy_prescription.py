# -*- coding: utf-8 -*-
"""Prescription management (BRD 5.4).

A prescription is a clinical authorisation to dispense, not a sales document. It is
therefore kept separate from sale.order: one prescription may be dispensed several
times (partial dispensing and refills), each dispensing producing its own sale order.

Lifecycle:
    draft -> to_verify -> verified -> partially_dispensed -> dispensed
                              \\-> cancelled / expired

Only a pharmacist may verify, and nothing can be dispensed before verification -
that gate is the point of the whole model.
"""

from datetime import timedelta

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

# A prescription with no explicit validity is treated as valid this long.
DEFAULT_VALIDITY_DAYS = 30


class PharmacyPrescription(models.Model):
    _name = 'pharmacy.prescription'
    _description = 'Prescription'
    # read another's prescriptions.
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'prescription_date desc, id desc'

    name = fields.Char(
        'Reference', copy=False, readonly=True, index=True, default=lambda s: _('New'))
    patient_id = fields.Many2one(
        'res.partner', string='Patient', required=True, tracking=True,
        domain="[('is_patient', '=', True)]")
    doctor_id = fields.Many2one(
        'res.partner', string='Prescriber', required=True, tracking=True,
        domain="[('is_doctor', '=', True)]")
    prescription_date = fields.Date(
        'Prescription Date', required=True, tracking=True,
        default=fields.Date.context_today)
    valid_until = fields.Date(
        'Valid Until', tracking=True,
        help='After this date the prescription can no longer be dispensed.')
    diagnosis = fields.Char('Diagnosis')
    notes = fields.Text('Notes')

    # --- source document ----------------------------------------------------
    is_digital = fields.Boolean(
        'Digital Prescription', default=False,
        help='Received electronically rather than as a paper script.')
    scan_file = fields.Binary('Scanned Prescription', attachment=True)
    scan_filename = fields.Char('Scan Filename')

    # --- refills ------------------------------------------------------------
    refills_allowed = fields.Integer(
        'Refills Allowed', default=0, tracking=True,
        help='How many times this prescription may be dispensed again after the '
             'first full dispensing.')
    refills_used = fields.Integer('Refills Used', readonly=True, copy=False)
    # Stored so the "Refillable" search filter can query it; it depends only on two
    # stored integers, so there is no recompute cost worth avoiding.
    refills_remaining = fields.Integer(
        compute='_compute_refills_remaining', store=True)

    line_ids = fields.One2many(
        'pharmacy.prescription.line', 'prescription_id', string='Medicines',
        copy=True)
    dispense_ids = fields.One2many(
        'pharmacy.dispense', 'prescription_id', string='Dispensings', readonly=True)
    dispense_count = fields.Integer(compute='_compute_dispense_count')

    state = fields.Selection([
        ('draft', 'Draft'),
        ('to_verify', 'To Verify'),
        ('verified', 'Verified'),
        ('partially_dispensed', 'Partially Dispensed'),
        ('dispensed', 'Dispensed'),
        ('expired', 'Expired'),
        ('cancelled', 'Cancelled'),
    ], default='draft', required=True, tracking=True, copy=False, index=True)

    verified_by_id = fields.Many2one(
        'res.users', string='Verified By', readonly=True, copy=False, tracking=True)
    verified_on = fields.Datetime('Verified On', readonly=True, copy=False)

    # --- derived flags ------------------------------------------------------
    has_controlled = fields.Boolean(
        compute='_compute_has_controlled', store=True,
        string='Contains Controlled Drug')
    patient_allergies = fields.Text(related='patient_id.allergies', readonly=True)

    # --- clinical screening (PRD 32, 33) -----------------------------------
    clinical_warning = fields.Text(
        'Clinical Warnings', compute='_compute_clinical_screen',
        help='Allergy and interaction findings for this patient and these medicines.')
    clinical_severity = fields.Selection([
        ('none', 'None'),
        ('low', 'Low'),
        ('moderate', 'Moderate'),
        ('high', 'High'),
        ('critical', 'Critical'),
    ], string='Highest Severity', compute='_compute_clinical_screen', default='none')
    clinical_ack_by_id = fields.Many2one('res.users', string='Warnings Reviewed By',
                                         readonly=True, copy=False)
    clinical_ack_on = fields.Datetime('Warnings Reviewed On', readonly=True, copy=False)
    clinical_ack_note = fields.Text('Clinical Review Note', copy=False)
    is_expired = fields.Boolean(compute='_compute_is_expired')
    fully_dispensed = fields.Boolean(compute='_compute_fully_dispensed')

    company_id = fields.Many2one(
        'res.company', default=lambda self: self.env.company, required=True)

    @api.depends('refills_allowed', 'refills_used')
    def _compute_refills_remaining(self):
        for rx in self:
            rx.refills_remaining = max(rx.refills_allowed - rx.refills_used, 0)

    @api.depends('dispense_ids')
    def _compute_dispense_count(self):
        for rx in self:
            rx.dispense_count = len(rx.dispense_ids)

    @api.depends('line_ids.product_id.is_controlled')
    def _compute_has_controlled(self):
        for rx in self:
            rx.has_controlled = any(rx.line_ids.mapped('product_id.is_controlled'))

    @api.depends('valid_until')
    def _compute_is_expired(self):
        today = fields.Date.today()
        for rx in self:
            rx.is_expired = bool(rx.valid_until and rx.valid_until < today)

    @api.depends('line_ids.qty_prescribed', 'line_ids.qty_dispensed')
    def _compute_fully_dispensed(self):
        for rx in self:
            rx.fully_dispensed = bool(rx.line_ids) and all(
                line.qty_dispensed >= line.qty_prescribed for line in rx.line_ids)

    @api.constrains('prescription_date', 'valid_until')
    def _check_dates(self):
        for rx in self:
            if rx.valid_until and rx.valid_until < rx.prescription_date:
                raise ValidationError(_(
                    "Prescription %s cannot expire before it was written.",
                    rx.name))

    @api.onchange('prescription_date')
    def _onchange_prescription_date(self):
        """Default the validity window so no prescription is silently open-ended."""
        if self.prescription_date and not self.valid_until:
            self.valid_until = self.prescription_date + timedelta(days=DEFAULT_VALIDITY_DAYS)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'pharmacy.prescription') or _('New')
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def action_submit(self):
        for rx in self:
            if not rx.line_ids:
                raise UserError(_("Add at least one medicine to %s before submitting.",
                                  rx.name))
        self.write({'state': 'to_verify'})

    @api.depends('patient_id', 'line_ids.product_id')
    def _compute_clinical_screen(self):
        """Screen the whole script at once, so a pharmacist sees every finding."""
        order = ['none', 'low', 'moderate', 'high', 'critical']
        for rx in self:
            products = rx.line_ids.mapped('product_id')
            findings = rx.patient_id.pharmacy_screen_products(products) \
                if rx.patient_id and products else []
            rx.clinical_warning = '\n'.join(
                '* %s' % f['message'] for f in findings) or False
            rx.clinical_severity = max(
                (f['severity'] for f in findings), key=order.index, default='none')

    def action_acknowledge_clinical(self):
        """Record that a pharmacist has read the warnings and still wants to proceed.

        This is the point of the whole screen: not to stop the medicine, which is a
        clinical call, but to make sure a named person made that call knowingly.
        """
        if not self.env.user.has_group('sahal_pharmacy.group_pharmacy_pharmacist'):
            raise UserError(_(
                "Only a pharmacist may sign off a clinical warning."))
        for rx in self:
            rx.write({
                'clinical_ack_by_id': self.env.user.id,
                'clinical_ack_on': fields.Datetime.now(),
            })
            rx.message_post(body=_(
                "<strong>Clinical warnings reviewed</strong> (%(severity)s)<br/>"
                "%(warnings)s<br/>%(note)s",
                severity=rx.clinical_severity,
                warnings=(rx.clinical_warning or '').replace('\n', '<br/>'),
                note=rx.clinical_ack_note or ''))
            if rx.clinical_severity in ('high', 'critical'):
                self.env['pharmacy.override'].log(
                    'clinical',
                    '%s\n%s' % (rx.clinical_warning or '', rx.clinical_ack_note or ''),
                    record=rx, partner=rx.patient_id)

    def action_verify(self):
        """Pharmacist sign-off. The gate that allows dispensing to start."""
        if not self.env.user.has_group('sahal_pharmacy.group_pharmacy_pharmacist'):
            raise UserError(_(
                "Only a pharmacist may verify a prescription. Ask a colleague with the "
                "Pharmacist role to review it."))
        for rx in self:
            if rx.state not in ('draft', 'to_verify'):
                raise UserError(_("%s is not awaiting verification.", rx.name))
            if rx.is_expired:
                raise UserError(_(
                    "%(name)s expired on %(date)s and cannot be verified.",
                    name=rx.name, date=rx.valid_until))
            # A high or critical finding must be signed off BY NAME before the script
            # can be verified. The warning is not a veto - the pharmacist may still
            # proceed - but nobody gets to say afterwards that they never saw it.
            if rx.clinical_severity in ('high', 'critical') and not rx.clinical_ack_by_id:
                raise UserError(_(
                    "%(name)s has %(severity)s clinical warnings that have not been "
                    "reviewed:\n\n%(warnings)s\n\nUse 'Review Warnings' to record "
                    "your decision, then verify.",
                    name=rx.name, severity=rx.clinical_severity,
                    warnings=rx.clinical_warning or ''))
        self.write({
            'state': 'verified',
            'verified_by_id': self.env.user.id,
            'verified_on': fields.Datetime.now(),
        })

    def action_reset_to_draft(self):
        for rx in self:
            if rx.dispense_ids:
                raise UserError(_(
                    "%s has already been dispensed and cannot go back to draft.",
                    rx.name))
        self.write({'state': 'draft', 'verified_by_id': False, 'verified_on': False})

    def action_cancel(self):
        self.write({'state': 'cancelled'})

    def action_view_dispensings(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Dispensings - %s', self.name),
            'res_model': 'pharmacy.dispense',
            'view_mode': 'list,form',
            'domain': [('prescription_id', '=', self.id)],
            'context': {'default_prescription_id': self.id},
        }

    def action_open_dispense_wizard(self):
        """Create a dispensing draft pre-filled with everything still outstanding."""
        self.ensure_one()
        self._assert_dispensable()
        dispense = self.env['pharmacy.dispense'].create({
            'prescription_id': self.id,
            'line_ids': [(0, 0, {
                'prescription_line_id': line.id,
                'product_id': line.product_id.id,
                'quantity': line.qty_remaining,
            }) for line in self.line_ids if line.qty_remaining > 0],
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Dispense - %s', self.name),
            'res_model': 'pharmacy.dispense',
            'res_id': dispense.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _assert_dispensable(self):
        """Raise unless this prescription may legally be dispensed right now."""
        self.ensure_one()
        if self.state == 'dispensed' and self.refills_remaining <= 0:
            raise UserError(_(
                "%s is fully dispensed with no refills left.", self.name))
        if self.state not in ('verified', 'partially_dispensed', 'dispensed'):
            raise UserError(_(
                "%s must be verified by a pharmacist before anything is dispensed.",
                self.name))
        if self.is_expired:
            raise UserError(_(
                "%(name)s expired on %(date)s and can no longer be dispensed.",
                name=self.name, date=self.valid_until))

    def _refresh_dispensing_state(self):
        """Move the prescription along after a dispensing is confirmed."""
        for rx in self:
            if rx.state in ('cancelled', 'expired'):
                continue
            if rx.fully_dispensed:
                rx.state = 'dispensed'
            elif rx.dispense_ids.filtered(lambda d: d.state == 'done'):
                rx.state = 'partially_dispensed'

    def action_start_refill(self):
        """Consume one refill and reopen the prescription for dispensing."""
        self.ensure_one()
        if self.state != 'dispensed':
            raise UserError(_("Only a fully dispensed prescription can be refilled."))
        if self.refills_remaining <= 0:
            raise UserError(_("%s has no refills remaining.", self.name))
        if self.is_expired:
            raise UserError(_("%s has expired; a new prescription is required.", self.name))
        self.refills_used += 1
        # Reset the dispensed counters so the next cycle starts from zero.
        self.line_ids._reset_for_refill()
        self.state = 'verified'
        self.message_post(body=_(
            "Refill %(used)s of %(allowed)s started.",
            used=self.refills_used, allowed=self.refills_allowed))

    @api.model
    def _cron_expire_prescriptions(self):
        """Flag prescriptions past their validity date so they stop being dispensable."""
        today = fields.Date.today()
        stale = self.sudo().search([
            ('state', 'in', ('draft', 'to_verify', 'verified', 'partially_dispensed')),
            ('valid_until', '<', today),
        ])
        stale.write({'state': 'expired'})
        return len(stale)


class PharmacyPrescriptionLine(models.Model):
    _name = 'pharmacy.prescription.line'
    _description = 'Prescription Line'


    prescription_id = fields.Many2one(
        'pharmacy.prescription', required=True, ondelete='cascade', index=True)
    product_id = fields.Many2one(
        'product.product', string='Medicine', required=True,
        domain="[('is_medicine', '=', True)]")
    qty_prescribed = fields.Float('Prescribed', default=1.0, required=True)
    # Sum of confirmed dispensings, so partial dispensing needs no manual bookkeeping.
    qty_dispensed = fields.Float(
        'Dispensed', compute='_compute_qty_dispensed', store=True)
    qty_remaining = fields.Float(compute='_compute_qty_dispensed', store=True)
    uom_id = fields.Many2one(related='product_id.uom_id', readonly=True)

    dosage = fields.Char('Dosage', help='e.g. 1 tablet')
    frequency = fields.Char('Frequency', help='e.g. twice daily')
    duration_days = fields.Integer('Duration (days)')
    instructions = fields.Char('Instructions', help='e.g. after food')
    substitution_allowed = fields.Boolean(
        'Generic Substitution', default=True,
        help='Whether an equivalent generic may be dispensed instead.')

    is_controlled = fields.Boolean(related='product_id.is_controlled', store=True)

    @api.depends('prescription_id.dispense_ids.state',
                 'prescription_id.dispense_ids.is_previous_cycle',
                 'prescription_id.dispense_ids.line_ids.quantity',
                 'qty_prescribed')
    def _compute_qty_dispensed(self):
        """Count only confirmed dispensings from the CURRENT refill cycle.

        Earlier cycles stay on file as the audit trail but must not count against
        this cycle's outstanding quantity, or a refill would look already dispensed.
        """
        for line in self:
            current = line.prescription_id.dispense_ids.filtered(
                lambda d: d.state == 'done' and not d.is_previous_cycle)
            dispensed = sum(
                dl.quantity for dl in current.mapped('line_ids')
                if dl.prescription_line_id.id == line.id)
            line.qty_dispensed = dispensed
            line.qty_remaining = max(line.qty_prescribed - dispensed, 0.0)

    @api.constrains('qty_prescribed')
    def _check_qty_positive(self):
        for line in self:
            if line.qty_prescribed <= 0:
                raise ValidationError(_("Prescribed quantity must be greater than zero."))

    def _reset_for_refill(self):
        """Detach previous dispensings so a refill cycle starts from zero.

        The dispensing records themselves are kept (they are the audit trail); the
        refill counter on the prescription is what distinguishes the cycles.
        """
        for line in self:
            line.prescription_id.dispense_ids.filtered(
                lambda d: d.state == 'done').write({'is_previous_cycle': True})
        self.invalidate_recordset(['qty_dispensed', 'qty_remaining'])
        self._compute_qty_dispensed()
