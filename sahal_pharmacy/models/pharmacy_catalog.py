# -*- coding: utf-8 -*-
"""Configurable pharmaceutical vocabulary (PRD 8, 9, 10).

Three things the PRD insists must be DATA, not code:

* **Dosage forms** - "administrators should be able to create additional dosage
  forms". They were a hard-coded Selection, so adding "Ampoule" meant a developer and
  a deploy. A pharmacy in one country lists sachets and vials; another lists pessaries.
* **Active ingredients** - a molecule is a record, not a word in a product name. Once
  it is a record, "what else contains paracetamol", allergy matching and substitution
  become queries instead of guesses.
* **Generics** - the PRD's Generic -> Brand A / Brand B / Brand C tree. Today the
  module holds `pharma_generic_name` and `brand_name` as two unrelated strings, so
  nothing can find the equivalents of a product. A generic is now a record that its
  brands point at, which is what makes substitution possible at all.

None of these replace anything Odoo provides - they are the pharmaceutical vocabulary
Odoo has no opinion about.
"""

from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


class PharmacyDosageForm(models.Model):
    _name = 'pharmacy.dosage.form'
    _description = 'Dosage Form'
    _order = 'sequence, name'

    name = fields.Char('Dosage Form', required=True, translate=True)
    code = fields.Char('Code', help='Short code used on labels and in exports.')
    sequence = fields.Integer(default=10)
    route_ids = fields.Many2many(
        'pharmacy.administration.route', string='Usual Routes',
        help='Routes this form is normally given by. Offered as a default on the '
             'product; never enforced, because exceptions are clinical decisions.')
    is_injectable = fields.Boolean(
        'Injectable', help='Injectables carry stricter handling and disposal rules.')
    active = fields.Boolean(default=True)
    product_count = fields.Integer(compute='_compute_product_count')

    _sql_constraints = [
        ('name_uniq', 'unique(name)', 'That dosage form already exists.'),
    ]

    def _compute_product_count(self):
        counts = {f.id: c for f, c in self.env['product.template']._read_group(
            [('dosage_form_id', 'in', self.ids)], ['dosage_form_id'], ['__count'])}
        for form in self:
            form.product_count = counts.get(form.id, 0)

    def action_view_products(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Products - %s', self.name),
            'res_model': 'product.template',
            'view_mode': 'list,form',
            'domain': [('dosage_form_id', '=', self.id)],
        }


class PharmacyAdministrationRoute(models.Model):
    _name = 'pharmacy.administration.route'
    _description = 'Route of Administration'
    _order = 'name'

    name = fields.Char('Route', required=True, translate=True)
    code = fields.Char('Code')
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('name_uniq', 'unique(name)', 'That route already exists.'),
    ]


class PharmacyActiveIngredient(models.Model):
    _name = 'pharmacy.active.ingredient'
    _description = 'Active Ingredient'
    _order = 'name'

    name = fields.Char('Ingredient', required=True, translate=True)
    code = fields.Char('Code')
    atc_code = fields.Char(
        'ATC Code', help='Anatomical Therapeutic Chemical classification, e.g. N02BE01 '
                         'for paracetamol.')
    therapeutic_class = fields.Char('Therapeutic Class')
    description = fields.Text('Description')
    # Allergy matching keys off this: a patient allergic to an ingredient is at risk
    # from EVERY product containing it, whatever the brand on the box.
    allergy_alias = fields.Char(
        'Allergy Aliases',
        help='Other names patients use for this substance, comma separated - e.g. '
             '"penicillin" for amoxicillin. Matched when screening a patient\'s '
             'recorded allergies.')
    is_antibiotic = fields.Boolean('Antibiotic')
    active = fields.Boolean(default=True)
    product_ids = fields.Many2many(
        'product.template', 'product_active_ingredient_rel', 'ingredient_id',
        'product_id', string='Products')
    product_count = fields.Integer(compute='_compute_product_count')

    _sql_constraints = [
        ('name_uniq', 'unique(name)', 'That active ingredient already exists.'),
    ]

    def _compute_product_count(self):
        for ingredient in self:
            ingredient.product_count = len(ingredient.product_ids)

    def action_view_products(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Products containing %s', self.name),
            'res_model': 'product.template',
            'view_mode': 'list,form',
            'domain': [('active_ingredient_ids', 'in', self.id)],
        }


class PharmacyGeneric(models.Model):
    """A generic medicine: the thing brands are versions OF.

    'Amoxicillin 500 mg capsule' is the generic; 'Amoxil', 'Moxatag' and the unbranded
    house product are its brands. Substitution, price comparison and "do we have
    anything equivalent in stock" all hang off this one relation.
    """
    _name = 'pharmacy.generic'
    _description = 'Generic Medicine'
    _order = 'name'

    name = fields.Char('Generic Name', required=True, index=True)
    active_ingredient_ids = fields.Many2many(
        'pharmacy.active.ingredient', string='Active Ingredients')
    strength = fields.Char('Strength', help='e.g. 500 mg - the strength brands must '
                                            'match to be considered equivalent.')
    dosage_form_id = fields.Many2one('pharmacy.dosage.form', string='Dosage Form')
    atc_code = fields.Char('ATC Code')
    therapeutic_class = fields.Char('Therapeutic Class')
    notes = fields.Text('Notes')
    active = fields.Boolean(default=True)
    product_ids = fields.One2many('product.template', 'generic_id', string='Brands')
    product_count = fields.Integer(compute='_compute_product_count')

    def _compute_product_count(self):
        counts = {g.id: c for g, c in self.env['product.template']._read_group(
            [('generic_id', 'in', self.ids)], ['generic_id'], ['__count'])}
        for generic in self:
            generic.product_count = counts.get(generic.id, 0)

    @api.constrains('name', 'active_ingredient_ids')
    def _check_has_ingredient(self):
        for generic in self:
            if not generic.active_ingredient_ids:
                raise ValidationError(_(
                    "Generic '%s' needs at least one active ingredient - that is what "
                    "makes two products equivalent.", generic.name))

    def action_view_products(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Brands of %s', self.name),
            'res_model': 'product.template',
            'view_mode': 'list,form',
            'domain': [('generic_id', '=', self.id)],
            'context': {'default_generic_id': self.id},
        }
