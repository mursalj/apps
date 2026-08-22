# -*- coding: utf-8 -*-
"""Patients, doctors, manufacturers and insurers (BRD 5.6).

All of them are contacts. Purchase history, prescription history and delivery
addresses therefore come free from sale orders and child contacts; only clinical and
insurance attributes are added here.

"""

from datetime import date

from odoo import models, fields, api, _

BLOOD_GROUPS = [
    ('a+', 'A+'), ('a-', 'A-'), ('b+', 'B+'), ('b-', 'B-'),
    ('ab+', 'AB+'), ('ab-', 'AB-'), ('o+', 'O+'), ('o-', 'O-'),
]


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # --- roles --------------------------------------------------------------
    is_patient = fields.Boolean('Is a Patient', default=False)
    is_doctor = fields.Boolean('Is a Doctor', default=False)
    is_pharma_manufacturer = fields.Boolean('Is a Manufacturer', default=False)
    is_insurer = fields.Boolean('Is an Insurer', default=False)

    # --- patient identity ---------------------------------------------------
    patient_code = fields.Char('Patient ID', copy=False, readonly=True, index=True)
    date_of_birth = fields.Date('Date of Birth')
    age = fields.Integer('Age', compute='_compute_age')
    gender = fields.Selection(
        [('male', 'Male'), ('female', 'Female'), ('other', 'Other')], string='Gender')
    blood_group = fields.Selection(BLOOD_GROUPS, string='Blood Group')

    # --- clinical -----------------------------------------------------------
    # Free text rather than a coded allergy list: coded allergy-to-molecule checking
    # needs a drug interaction database, which is out of scope here. The banner on
    # the prescription form makes sure a pharmacist still SEES this before dispensing.
    allergies = fields.Text(
        'Allergies', help='Known drug and substance allergies. Shown as a warning '
                          'whenever a prescription is dispensed to this patient.')
    has_allergies = fields.Boolean(compute='_compute_has_allergies', store=True)
    chronic_conditions = fields.Text('Chronic Conditions')
    medical_notes = fields.Text('Medical History')

    # --- insurance ----------------------------------------------------------
    insurer_id = fields.Many2one(
        'res.partner', string='Insurance Provider',
        domain="[('is_insurer', '=', True)]")
    insurance_member_no = fields.Char('Policy / Member No.')
    insurance_valid_until = fields.Date('Insurance Valid Until')
    insurance_coverage_pct = fields.Float(
        'Coverage %', help='Share of the bill the insurer pays, 0-100. Used to split '
                           'a dispensing between insurer and patient.')
    insurance_is_valid = fields.Boolean(compute='_compute_insurance_is_valid')

    # --- emergency ----------------------------------------------------------
    emergency_contact_name = fields.Char('Emergency Contact')
    emergency_contact_phone = fields.Char('Emergency Phone')

    # --- doctor -------------------------------------------------------------
    medical_license_no = fields.Char('Medical Licence No.')
    specialty = fields.Char('Specialty')

    # --- history ------------------------------------------------------------
    prescription_ids = fields.One2many(
        'pharmacy.prescription', 'patient_id', string='Prescriptions')
    prescription_count = fields.Integer(compute='_compute_prescription_count')

    @api.depends('date_of_birth')
    def _compute_age(self):
        today = date.today()
        for partner in self:
            dob = partner.date_of_birth
            if dob:
                partner.age = today.year - dob.year - (
                    (today.month, today.day) < (dob.month, dob.day))
            else:
                partner.age = 0

    @api.depends('allergies')
    def _compute_has_allergies(self):
        for partner in self:
            partner.has_allergies = bool(partner.allergies and partner.allergies.strip())

    @api.depends('insurance_valid_until', 'insurer_id')
    def _compute_insurance_is_valid(self):
        today = fields.Date.today()
        for partner in self:
            partner.insurance_is_valid = bool(
                partner.insurer_id and (
                    not partner.insurance_valid_until
                    or partner.insurance_valid_until >= today))

    @api.depends('prescription_ids')
    def _compute_prescription_count(self):
        for partner in self:
            partner.prescription_count = len(partner.prescription_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('is_patient') and not vals.get('patient_code'):
                vals['patient_code'] = self.env['ir.sequence'].next_by_code(
                    'pharmacy.patient') or _('New')
        return super().create(vals_list)

    def write(self, vals):
        res = super().write(vals)
        # A contact promoted to patient afterwards still needs an ID.
        if vals.get('is_patient'):
            for partner in self.filtered(lambda p: not p.patient_code):
                partner.patient_code = self.env['ir.sequence'].next_by_code(
                    'pharmacy.patient') or _('New')
        return res

    def action_view_prescriptions(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Prescriptions — %s', self.display_name),
            'res_model': 'pharmacy.prescription',
            'view_mode': 'list,form',
            'domain': [('patient_id', '=', self.id)],
            'context': {'default_patient_id': self.id},
        }
