# -*- coding: utf-8 -*-
"""Clinical safety: allergies, interactions and substitution (PRD 32, 33, 34, 67).

Three checks a pharmacist performs before handing medicine over, and which the module
could not perform because it had no idea what a product CONTAINS:

* **Allergy screening.** The patient's allergies were free text, shown as a banner and
  matched against nothing. "Penicillin" written on a patient record did not stop
  amoxicillin being dispensed, because the two strings do not match. Now that active
  ingredients are records (PH1), the screen matches on the MOLECULE and on the aliases
  recorded against it, which is the only way to catch a brand nobody typed.

* **Interactions.** A pairing between two ingredients with a severity and a recommended
  action. Checked across everything on the prescription, and against what the patient
  is already taking.

* **Substitution.** Dispensing a different product from the one prescribed is a clinical
  decision that has to be recorded — who authorised it, why, and what was swapped.

DELIBERATE LIMIT, stated plainly because it matters: this is a warning system driven by
data the pharmacy enters itself. It is not a clinical database and it does not replace a
pharmacist's judgement (PRD 32). Nothing here silently substitutes a prescription
medicine or overrides an alert on its own.
"""

import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

SEVERITIES = [
    ('low', 'Low'),
    ('moderate', 'Moderate'),
    ('high', 'High'),
    ('critical', 'Critical'),
]

# Severities a pharmacist must actively acknowledge rather than merely see.
BLOCKING_SEVERITIES = ('high', 'critical')


class PharmacyDrugInteraction(models.Model):
    _name = 'pharmacy.drug.interaction'
    _description = 'Drug Interaction'

    _order = 'severity desc, id desc'
    _rec_name = 'display_summary'

    ingredient_a_id = fields.Many2one(
        'pharmacy.active.ingredient', string='Ingredient', required=True, index=True,
        ondelete='cascade')
    ingredient_b_id = fields.Many2one(
        'pharmacy.active.ingredient', string='Interacts With', required=True, index=True,
        ondelete='cascade')
    severity = fields.Selection(SEVERITIES, required=True, default='moderate')
    description = fields.Text('What Happens', required=True)
    recommended_action = fields.Text('Recommended Action')
    source = fields.Char('Source', help='Reference the pharmacy can cite.')
    date_effective = fields.Date('Effective From', default=fields.Date.context_today)
    active = fields.Boolean(default=True)
    display_summary = fields.Char(compute='_compute_display_summary', store=True)
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company)

    _sql_constraints = [
        ('pair_uniq', 'unique(ingredient_a_id, ingredient_b_id, company_id)',
         'That interaction pair is already recorded.'),
    ]

    @api.depends('ingredient_a_id', 'ingredient_b_id', 'severity')
    def _compute_display_summary(self):
        labels = dict(SEVERITIES)
        for record in self:
            record.display_summary = '%s + %s (%s)' % (
                record.ingredient_a_id.name or '?', record.ingredient_b_id.name or '?',
                labels.get(record.severity, ''))

    @api.constrains('ingredient_a_id', 'ingredient_b_id')
    def _check_not_self(self):
        for record in self:
            if record.ingredient_a_id == record.ingredient_b_id:
                raise ValidationError(_(
                    "An ingredient cannot be recorded as interacting with itself."))

    @api.model
    def find_for_ingredients(self, ingredients):
        """Every recorded interaction among this set of ingredients.

        Order-independent: the pair is stored one way round but a prescription may list
        them either way, so both directions are searched.
        """
        if len(ingredients) < 2:
            return self.browse()
        ids = ingredients.ids
        return self.search([
            ('ingredient_a_id', 'in', ids), ('ingredient_b_id', 'in', ids),
        ])


class PharmacySubstitution(models.Model):
    _name = 'pharmacy.substitution'
    _description = 'Medicine Substitution'
    _inherit = ['mail.thread']
    _order = 'create_date desc'

    prescription_id = fields.Many2one('pharmacy.prescription', string='Prescription',
                                      required=True, ondelete='cascade', index=True)
    patient_id = fields.Many2one(related='prescription_id.patient_id', store=True,
                                 readonly=True)
    original_product_id = fields.Many2one('product.product', string='Prescribed',
                                          required=True)
    substitute_product_id = fields.Many2one('product.product', string='Dispensed',
                                            required=True)
    quantity = fields.Float('Quantity', default=1.0)
    reason = fields.Selection([
        ('out_of_stock', 'Prescribed Item Out of Stock'),
        ('generic', 'Generic Equivalent'),
        ('cost', 'Cost to Patient'),
        ('recall', 'Prescribed Batch Recalled'),
        ('prescriber', 'Prescriber Agreed'),
        ('other', 'Other'),
    ], required=True, default='out_of_stock')
    note = fields.Text('Notes')
    pharmacist_id = fields.Many2one('res.users', string='Authorised By', required=True,
                                    default=lambda s: s.env.user, readonly=True)
    substituted_on = fields.Datetime('Authorised On', default=fields.Datetime.now,
                                     readonly=True)
    is_equivalent = fields.Boolean(
        'Same Generic', compute='_compute_is_equivalent', store=True,
        help='True when both products share a generic — the only case the system can '
             'call clinically equivalent on its own.')
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company)

    @api.depends('original_product_id', 'substitute_product_id')
    def _compute_is_equivalent(self):
        for record in self:
            original = record.original_product_id.product_tmpl_id.generic_id
            substitute = record.substitute_product_id.product_tmpl_id.generic_id
            record.is_equivalent = bool(original and original == substitute)

    @api.model_create_multi
    def create(self, vals_list):
        """Only a pharmacist may record a substitution, and never silently.

        The PRD is explicit (34): the system must not substitute a prescription medicine
        on its own. So this record can only be written by a human with the authority to
        make that call, and it is written to the prescription's chatter where the
        prescriber's original choice is visible next to it.
        """
        if not self.env.su and not self.env.user.has_group(
                'sahal_pharmacy.group_pharmacy_pharmacist'):
            raise UserError(_(
                "Substituting a prescribed medicine is a pharmacist's decision."))
        records = super().create(vals_list)
        for record in records:
            record.prescription_id.message_post(body=_(
                "<strong>Substitution</strong>: %(orig)s dispensed as %(sub)s "
                "(%(reason)s)%(equiv)s.",
                orig=record.original_product_id.display_name,
                sub=record.substitute_product_id.display_name,
                reason=dict(record._fields['reason'].selection)[record.reason],
                equiv=_(' — same generic') if record.is_equivalent
                else _(' — NOT the same generic')))
        return records


class ResPartner(models.Model):
    _inherit = 'res.partner'

    allergy_ingredient_ids = fields.Many2many(
        'pharmacy.active.ingredient', 'partner_allergy_ingredient_rel', 'partner_id',
        'ingredient_id', string='Allergic To',
        help='Coded allergies, matched against what a medicine actually contains. The '
             'free-text Allergies note stays for anything that cannot be coded.')

    def pharmacy_screen_products(self, products):
        """Clinical screen of these products for this patient.

        Returns a list of findings, each a dict with a `kind` ('allergy' or
        'interaction'), a severity and a human sentence. Returning findings rather than
        raising is deliberate: the caller decides whether this is a warning to show or a
        gate to block, and a pharmacist must be able to see everything at once rather
        than one exception at a time.
        """
        self.ensure_one()
        findings = []
        if not products:
            return findings

        templates = products.mapped('product_tmpl_id')
        ingredients = templates.mapped('active_ingredient_ids')

        # --- allergies, matched on the molecule ---------------------------
        for ingredient in ingredients & self.allergy_ingredient_ids:
            affected = templates.filtered(lambda t: ingredient in t.active_ingredient_ids)
            findings.append({
                'kind': 'allergy',
                'severity': 'critical',
                'ingredient_id': ingredient.id,
                'message': _(
                    "%(patient)s is recorded as allergic to %(ingredient)s, which is in "
                    "%(products)s.",
                    patient=self.display_name, ingredient=ingredient.name,
                    products=', '.join(affected.mapped('display_name'))),
            })

        # --- free-text allergies, matched on name and alias ----------------
        # A pharmacy that has not coded its allergies yet still gets a hit, which is
        # what stops this being useless on day one.
        noted = (self.allergies or '').lower()
        if noted:
            for ingredient in ingredients - self.allergy_ingredient_ids:
                aliases = [ingredient.name.lower()]
                aliases += [a.strip().lower()
                            for a in (ingredient.allergy_alias or '').split(',')
                            if a.strip()]
                if any(alias and alias in noted for alias in aliases):
                    findings.append({
                        'kind': 'allergy',
                        'severity': 'high',
                        'ingredient_id': ingredient.id,
                        'message': _(
                            "%(patient)s's recorded allergies mention %(ingredient)s. "
                            "Confirm before dispensing.",
                            patient=self.display_name, ingredient=ingredient.name),
                    })

        # --- interactions between what is being dispensed ------------------
        interactions = self.env['pharmacy.drug.interaction'].find_for_ingredients(
            ingredients)
        for interaction in interactions:
            findings.append({
                'kind': 'interaction',
                'severity': interaction.severity,
                'interaction_id': interaction.id,
                'message': _(
                    "%(a)s and %(b)s interact (%(severity)s): %(what)s %(action)s",
                    a=interaction.ingredient_a_id.name,
                    b=interaction.ingredient_b_id.name,
                    severity=dict(SEVERITIES)[interaction.severity],
                    what=interaction.description or '',
                    action=interaction.recommended_action or ''),
            })
        return findings
