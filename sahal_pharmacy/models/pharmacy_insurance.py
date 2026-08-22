# -*- coding: utf-8 -*-
"""Insurance claims and the copay split (PRD 44).

The patient record already carried an insurer, a policy number and a coverage
percentage. Nothing used them: a dispensing charged the patient the whole amount, and
the pharmacy tracked what the insurer owed outside the system.

A claim answers the two questions that follow every insured dispensing:

    Retail price   $100
    Insurer pays    $80   -> invoiced to the insurer, and chased like any receivable
    Patient pays    $20   -> the copay, taken at the counter

Deliberately manual at the edges. The claim is created from a dispensing, but SUBMITTING
it and recording the insurer's answer are human steps: no insurer on earth accepts a
claim because an ERP decided it was valid, and pretending otherwise would put fictional
receivables on the books.
"""

import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class PharmacyInsuranceClaim(models.Model):
    _name = 'pharmacy.insurance.claim'
    _description = 'Insurance Claim'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'claim_date desc, id desc'

    name = fields.Char('Claim', copy=False, readonly=True, index=True,
                       default=lambda s: _('New'))
    patient_id = fields.Many2one('res.partner', string='Patient', required=True,
                                 tracking=True)
    insurer_id = fields.Many2one('res.partner', string='Insurer', required=True,
                                 domain="[('is_insurer', '=', True)]", tracking=True)
    policy_no = fields.Char('Policy / Member No.')
    claim_date = fields.Date('Claim Date', default=fields.Date.context_today,
                             required=True)

    dispense_id = fields.Many2one('pharmacy.dispense', string='Dispensing')
    sale_order_id = fields.Many2one('sale.order', string='Sale')

    total_amount = fields.Monetary('Total', currency_field='currency_id', required=True)
    coverage_pct = fields.Float('Coverage %', help='Share the insurer pays, 0-100.')
    covered_amount = fields.Monetary('Insurer Pays', currency_field='currency_id',
                                     compute='_compute_split', store=True, readonly=False)
    patient_amount = fields.Monetary('Patient Pays (Copay)',
                                     currency_field='currency_id',
                                     compute='_compute_split', store=True)

    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('invoiced', 'Invoiced'),
        ('paid', 'Paid'),
        ('cancel', 'Cancelled'),
    ], default='draft', required=True, tracking=True)
    submitted_on = fields.Date('Submitted On', readonly=True, copy=False)
    response_on = fields.Date('Answered On', readonly=True, copy=False)
    response_note = fields.Text('Insurer Response')
    rejection_reason = fields.Char('Rejection Reason', tracking=True)
    invoice_id = fields.Many2one('account.move', string='Insurer Invoice', readonly=True,
                                 copy=False)

    currency_id = fields.Many2one('res.currency', related='company_id.currency_id',
                                  readonly=True)
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company,
                                 required=True)

    @api.depends('total_amount', 'coverage_pct')
    def _compute_split(self):
        for claim in self:
            covered = claim.total_amount * (claim.coverage_pct or 0.0) / 100.0
            claim.covered_amount = covered
            claim.patient_amount = claim.total_amount - covered

    @api.constrains('coverage_pct')
    def _check_coverage(self):
        for claim in self:
            if not 0 <= (claim.coverage_pct or 0) <= 100:
                raise ValidationError(_("Coverage must be between 0 and 100 percent."))

    @api.constrains('covered_amount', 'total_amount')
    def _check_covered_amount(self):
        for claim in self:
            if claim.covered_amount > claim.total_amount:
                raise ValidationError(_(
                    "The insurer cannot be claimed more than the bill: %(covered)s "
                    "against a total of %(total)s.",
                    covered=claim.covered_amount, total=claim.total_amount))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'pharmacy.insurance.claim') or _('New')
        return super().create(vals_list)

    @api.onchange('patient_id')
    def _onchange_patient(self):
        """Take the cover from the patient's own record, which already holds it."""
        if not self.patient_id:
            return
        self.insurer_id = self.patient_id.insurer_id
        self.policy_no = self.patient_id.insurance_member_no
        self.coverage_pct = self.patient_id.insurance_coverage_pct

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def action_submit(self):
        for claim in self:
            if claim.state != 'draft':
                raise UserError(_("Only a draft claim can be submitted."))
            if not claim.covered_amount:
                raise UserError(_(
                    "%s claims nothing from the insurer. Set the coverage before "
                    "submitting it.", claim.name))
            claim.write({'state': 'submitted',
                         'submitted_on': fields.Date.context_today(claim)})

    def action_approve(self):
        for claim in self:
            if claim.state != 'submitted':
                raise UserError(_("Only a submitted claim can be approved."))
            claim.write({'state': 'approved',
                         'response_on': fields.Date.context_today(claim)})

    def action_reject(self):
        for claim in self:
            if not claim.rejection_reason:
                raise UserError(_(
                    "Record why the insurer rejected %s — the patient will ask, and so "
                    "will the next claim.", claim.name))
            claim.write({'state': 'rejected',
                         'response_on': fields.Date.context_today(claim)})

    def action_invoice_insurer(self):
        """Bill the insurer for their share, once they have agreed to it."""
        self.ensure_one()
        if self.invoice_id:
            raise UserError(_("Invoice %s already exists for this claim.",
                              self.invoice_id.name or self.invoice_id.id))
        if self.state != 'approved':
            raise UserError(_(
                "Invoice the insurer only once the claim is approved: billing an "
                "unapproved claim books a receivable that may never be paid."))
        product = self.env['product.product'].sudo().search(
            [('default_code', '=', 'PHARMA-CLAIM')], limit=1)
        if not product:
            product = self.env['product.product'].sudo().create({
                'name': _('Insurance Claim'), 'default_code': 'PHARMA-CLAIM',
                'type': 'service', 'sale_ok': True, 'purchase_ok': False,
            })
        move = self.env['account.move'].with_company(self.company_id).create({
            'move_type': 'out_invoice',
            'partner_id': self.insurer_id.id,
            'invoice_date': fields.Date.context_today(self),
            'invoice_origin': self.name,
            'company_id': self.company_id.id,
            'invoice_line_ids': [(0, 0, {
                'product_id': product.id,
                'name': _('%(claim)s — %(patient)s (policy %(policy)s)',
                          claim=self.name, patient=self.patient_id.display_name,
                          policy=self.policy_no or '-'),
                'quantity': 1.0,
                'price_unit': self.covered_amount,
            })],
        })
        self.write({'invoice_id': move.id, 'state': 'invoiced'})
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': move.id,
        }

    def action_cancel(self):
        for claim in self:
            if claim.invoice_id and claim.invoice_id.state == 'posted':
                raise UserError(_(
                    "Invoice %s is posted. Reverse it before cancelling the claim.",
                    claim.invoice_id.name))
            claim.state = 'cancel'


class PharmacyDispense(models.Model):
    _inherit = 'pharmacy.dispense'

    insurance_claim_ids = fields.One2many('pharmacy.insurance.claim', 'dispense_id',
                                          string='Insurance Claims')
    claim_count = fields.Integer(compute='_compute_claim_count')
    patient_is_insured = fields.Boolean(compute='_compute_patient_is_insured')

    def _compute_claim_count(self):
        for dispense in self:
            dispense.claim_count = len(dispense.insurance_claim_ids)

    @api.depends('patient_id')
    def _compute_patient_is_insured(self):
        for dispense in self:
            patient = dispense.patient_id
            dispense.patient_is_insured = bool(
                patient.insurer_id and patient.insurance_is_valid
                and patient.insurance_coverage_pct)

    def action_create_insurance_claim(self):
        """Split this dispensing between the insurer and the patient."""
        self.ensure_one()
        patient = self.patient_id
        if not patient.insurer_id:
            raise UserError(_("%s has no insurer on record.", patient.display_name))
        if not patient.insurance_is_valid:
            raise UserError(_(
                "%(patient)s's cover expired on %(date)s. Update the policy before "
                "claiming against it.", patient=patient.display_name,
                date=patient.insurance_valid_until))
        if self.insurance_claim_ids.filtered(lambda c: c.state != 'cancel'):
            raise UserError(_("A claim already exists for %s.", self.name))
        if not self.sale_order_id:
            raise UserError(_(
                "Confirm the dispensing first: the claim is for what was actually "
                "supplied."))
        claim = self.env['pharmacy.insurance.claim'].create({
            'patient_id': patient.id,
            'insurer_id': patient.insurer_id.id,
            'policy_no': patient.insurance_member_no,
            'coverage_pct': patient.insurance_coverage_pct,
            'total_amount': self.sale_order_id.amount_total,
            'dispense_id': self.id,
            'sale_order_id': self.sale_order_id.id,
            'company_id': self.company_id.id,
        })
        self.message_post(body=_(
            "Insurance claim %(claim)s raised: insurer %(covered)s, patient %(patient)s.",
            claim=claim.name, covered=claim.covered_amount,
            patient=claim.patient_amount))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'pharmacy.insurance.claim',
            'view_mode': 'form',
            'res_id': claim.id,
        }

    def action_view_claims(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Claims — %s', self.name),
            'res_model': 'pharmacy.insurance.claim',
            'view_mode': 'list,form',
            'domain': [('dispense_id', '=', self.id)],
        }
