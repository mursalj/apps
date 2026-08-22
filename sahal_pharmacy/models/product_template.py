# -*- coding: utf-8 -*-
"""Medicine master (BRD 5.1).

Medicines are ordinary products. Everything Odoo already models is used as-is and
NOT duplicated here:

    Barcode           -> product.barcode          Selling price -> list_price
    SKU               -> product.default_code     Cost price    -> standard_price
    Category          -> categ_id                 VAT/Tax       -> taxes_id
    Unit of Measure   -> uom_id                   Images        -> image_1920
    Min/Max/Reorder   -> stock.warehouse.orderpoint
    Shelf life        -> product_expiry.expiration_time (days)
    Supplier          -> seller_ids (product.supplierinfo)

Only genuinely pharmaceutical attributes are added below. A QR code is not a stored
field either: Odoo renders one from `barcode` with the barcode widget/report.
"""

from odoo import models, fields, api, _
from odoo.exceptions import ValidationError, UserError

DOSAGE_FORMS = [
    ('tablet', 'Tablet'),
    ('capsule', 'Capsule'),
    ('syrup', 'Syrup'),
    ('suspension', 'Suspension'),
    ('solution', 'Solution'),
    ('injection', 'Injection'),
    ('infusion', 'Infusion'),
    ('cream', 'Cream'),
    ('ointment', 'Ointment'),
    ('gel', 'Gel'),
    ('drops', 'Drops'),
    ('inhaler', 'Inhaler'),
    ('suppository', 'Suppository'),
    ('patch', 'Transdermal Patch'),
    ('powder', 'Powder'),
    ('other', 'Other'),
]

# What kind of product this is. A pharmacy sells more than drugs, and the rules differ
# per kind — this is the field every downstream rule reads.
PHARMA_TYPES = [
    ('prescription', 'Prescription Medicine'),
    ('otc', 'Over-the-Counter Medicine'),
    ('supplement', 'Supplement / Vitamin'),
    ('device', 'Medical Device'),
    ('cosmetic', 'Cosmetic / Personal Care'),
    ('other', 'Other'),
]

# Types that are NOT medicines in the regulatory sense: they can never require a
# prescription and must stay out of the controlled register.
NON_MEDICINE_TYPES = ('supplement', 'device', 'cosmetic', 'other')

# Types with no shelf life. They are still batch-tracked — a recall has to reach them —
# but stamping them with an expiry date would make every unit arrive expired.
NON_EXPIRING_TYPES = ('device', 'other')

STRENGTH_UNITS = [
    ('mg', 'mg'), ('g', 'g'), ('mcg', 'mcg'), ('ml', 'mL'), ('l', 'L'),
    ('mg_ml', 'mg/mL'), ('mcg_ml', 'mcg/mL'), ('g_l', 'g/L'),
    ('iu', 'IU'), ('iu_ml', 'IU/mL'), ('pct', '%'), ('meq', 'mEq'),
]

# Controlled-substance schedules. Kept generic (I-V) because the exact register
# differs per jurisdiction; the register itself is what regulators ask for.
CONTROLLED_SCHEDULES = [
    ('i', 'Schedule I'),
    ('ii', 'Schedule II'),
    ('iii', 'Schedule III'),
    ('iv', 'Schedule IV'),
    ('v', 'Schedule V'),
]


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    is_medicine = fields.Boolean(
        string='Is a Pharmacy Product', default=False,
        help='Tick to expose the pharmaceutical attributes and include this product '
             'in pharmacy catalogues, dispensing and regulatory reports. Covers '
             'medicines, supplements and medical devices alike.')

    # A pharmacy does not only sell drugs. Supplements, devices and cosmetics live on
    # the same shelves, are bought from the same suppliers and are rung up on the same
    # till — but they are NOT prescription items, and treating everything as a medicine
    # is how a vitamin ends up demanding a prescription. One field decides which rules
    # apply; everything downstream keys off it rather than guessing from flags.
    # Deliberately NO default. A field default is written to every existing row when
    # the column is created, which would classify the whole catalogue before the
    # migration could look at it — including turning prescription-only medicines into
    # over-the-counter ones. New pharmacy products get their default from the Medicines
    # action's context instead.
    pharma_type = fields.Selection(PHARMA_TYPES, string='Product Type',
                                   tracking=True, index=True,
                                   help='What kind of pharmacy product this is. Drives '
                                        'whether a prescription is required and how it '
                                        'is reported.')
    is_otc = fields.Boolean('Over the Counter', compute='_compute_is_otc', store=True,
                            help='Sellable without a prescription.')

    # --- identification ---------------------------------------------------
    # Named pharma_generic_name, not generic_name, on purpose:
    # dental_clinical_management already declares product.template.generic_name with
    # required=True. Sharing that field would make the flag depend on module load
    # order and would drag a third-party module's (broken) NOT NULL requirement into
    # every pharmacy product. A distinct name keeps this module's behaviour its own.
    pharma_generic_name = fields.Char(
        'Generic Name', index=True,
        help='International non-proprietary name, e.g. Paracetamol. Searched '
             'alongside the trade name so staff can find a medicine either way.')
    brand_name = fields.Char('Brand Name', index=True)
    manufacturer_id = fields.Many2one(
        'res.partner', string='Manufacturer',
        domain="[('is_pharma_manufacturer', '=', True)]")
    # Kept as a Selection for the data already captured in it, but the authoritative
    # field is dosage_form_id: the PRD requires administrators to add their own forms,
    # which a Selection cannot do without a code change and a deploy.
    dosage_form = fields.Selection(DOSAGE_FORMS, string='Dosage Form (legacy)')
    dosage_form_id = fields.Many2one('pharmacy.dosage.form', string='Dosage Form',
                                     index=True)
    route_id = fields.Many2one('pharmacy.administration.route',
                               string='Route of Administration')
    strength = fields.Char('Strength', compute='_compute_strength', store=True,
                           readonly=False,
                           help='Printed on labels and bills. Computed from the value '
                                'and unit below, but can be overridden for anything '
                                'those two cannot express.')
    strength_value = fields.Float('Strength Value',
                                  help='e.g. 500 for a 500 mg tablet.')
    strength_uom = fields.Selection(STRENGTH_UNITS, string='Strength Unit')
    concentration = fields.Char(
        'Concentration', help='For liquids and injectables, e.g. 100 IU/mL.')
    active_ingredient_ids = fields.Many2many(
        'pharmacy.active.ingredient', 'product_active_ingredient_rel', 'product_id',
        'ingredient_id', string='Active Ingredients',
        help='What the product actually contains. Drives allergy screening and '
             'equivalence — a brand name cannot.')
    generic_id = fields.Many2one(
        'pharmacy.generic', string='Generic', index=True,
        help='The generic this product is a brand of. Products sharing a generic are '
             'candidates for substitution.')
    atc_code = fields.Char('ATC Code', index=True)
    therapeutic_class = fields.Char('Therapeutic Class')
    drug_class = fields.Char('Drug Class')
    gtin = fields.Char('GTIN', help='Global Trade Item Number, when it differs from '
                                    'the scanned barcode.')
    country_of_origin_id = fields.Many2one('res.country', string='Country of Origin')
    pack_size = fields.Char('Pack Size', help='e.g. 10 x 10 tablets, 100 ml bottle')

    # --- dispensing control -----------------------------------------------
    requires_prescription = fields.Boolean(
        'Prescription Required', default=False,
        help='Cannot be sold or dispensed without a verified prescription.')
    is_controlled = fields.Boolean(
        'Controlled Drug', default=False,
        help='Restricted substance: dispensing requires a pharmacist, and every '
             'movement is written to the controlled drugs register.')
    controlled_schedule = fields.Selection(
        CONTROLLED_SCHEDULES, string='Schedule',
        help='Regulatory schedule this substance falls under.')

    # --- storage ------------------------------------------------------------
    # --- handling flags (PRD 7) ------------------------------------------------
    is_antibiotic = fields.Boolean(
        'Antibiotic', help='Reported separately: antibiotic stewardship is a standing '
                           'regulatory concern almost everywhere.')
    is_hazardous = fields.Boolean('Hazardous')
    is_high_alert = fields.Boolean(
        'High-Alert Medicine',
        help='A medicine that causes significant harm when used in error — insulin, '
             'anticoagulants, concentrated electrolytes. Warns at dispensing.')
    is_pediatric = fields.Boolean('Pediatric')
    is_veterinary = fields.Boolean('Veterinary')
    temperature_min = fields.Float('Min Temperature (°C)')
    temperature_max = fields.Float('Max Temperature (°C)')

    shelf_life_warning = fields.Boolean(
        'Missing Shelf Life', compute='_compute_shelf_life_warning',
        help='This medicine expires but no shelf life is set, so every batch received '
             'would be dated today and treated as expired.')

    requires_cold_storage = fields.Boolean(
        'Cold Chain', default=False,
        help='Must be kept refrigerated; shown on picking and delivery documents.')
    storage_instructions = fields.Text(
        'Storage Instructions',
        help='e.g. Store below 25 °C, protect from light.')

    @api.depends('pharma_type', 'requires_prescription')
    def _compute_is_otc(self):
        """Over the counter = a pharmacy product that needs no prescription."""
        for product in self:
            product.is_otc = bool(
                product.is_medicine and not product.requires_prescription)

    @api.depends('strength_value', 'strength_uom')
    def _compute_strength(self):
        """Render the label text from the structured value, without losing overrides."""
        labels = dict(STRENGTH_UNITS)
        for product in self:
            if not product.strength_value or not product.strength_uom:
                # Leave whatever is there: legacy rows hold free text like "10 mg/5 ml".
                product.strength = product.strength
                continue
            value = product.strength_value
            rendered = int(value) if float(value).is_integer() else value
            product.strength = '%s %s' % (rendered, labels.get(product.strength_uom, ''))

    @api.constrains('pharma_type', 'requires_prescription', 'is_controlled')
    def _check_type_consistency(self):
        """A supplement cannot be a prescription item, and a device is not a drug.

        Without this the classification is decorative: nothing stops someone flagging a
        vitamin prescription-only, and then the till refuses to sell it.
        """
        for product in self:
            if product.pharma_type in NON_MEDICINE_TYPES:
                if product.requires_prescription:
                    raise ValidationError(_(
                        "'%(name)s' is classified as %(type)s, which cannot require a "
                        "prescription. Classify it as a prescription medicine instead.",
                        name=product.display_name,
                        type=dict(PHARMA_TYPES)[product.pharma_type]))
                if product.is_controlled:
                    raise ValidationError(_(
                        "'%(name)s' is classified as %(type)s and cannot be a "
                        "controlled substance.",
                        name=product.display_name,
                        type=dict(PHARMA_TYPES)[product.pharma_type]))
            if product.pharma_type == 'prescription' and not product.requires_prescription:
                raise ValidationError(_(
                    "'%s' is classified as a prescription medicine, so it must require "
                    "a prescription.", product.display_name))

    @api.depends('is_medicine', 'use_expiration_date', 'expiration_time')
    def _compute_shelf_life_warning(self):
        """Flag a medicine that expires but has no shelf life.

        product_expiry stamps a new lot with `expiration_date = today + expiration_time`.
        With expiration_time left at zero that is TODAY, so every batch is expired on
        arrival and the expiry block refuses to sell any of it — which looks like the
        expiry rules misfiring when the real fault is an unset field.

        A WARNING, not a constraint, on purpose. A constraint would refuse every write to
        the medicines already carrying this mistake, so the first person to touch one
        would be stopped from fixing anything at all — including the shelf life.
        """
        for product in self:
            product.shelf_life_warning = bool(
                product.is_medicine and product.use_expiration_date
                and not product.expiration_time)

    @api.constrains('temperature_min', 'temperature_max')
    def _check_temperature_range(self):
        for product in self:
            if product.temperature_min and product.temperature_max and \
                    product.temperature_min > product.temperature_max:
                raise ValidationError(_(
                    "'%s': the minimum storage temperature is above the maximum.",
                    product.display_name))

    @api.onchange('pharma_type')
    def _onchange_pharma_type(self):
        """Make the classification actually set the rules, not just describe them."""
        if self.pharma_type == 'prescription':
            self.requires_prescription = True
        elif self.pharma_type in NON_MEDICINE_TYPES:
            self.requires_prescription = False
            self.is_controlled = False
            self.controlled_schedule = False
        if self.pharma_type in ('prescription', 'otc', 'supplement', 'device',
                                'cosmetic'):
            self.is_medicine = True

    @api.onchange('generic_id')
    def _onchange_generic(self):
        """Inherit the generic's pharmacology unless this brand states its own."""
        if not self.generic_id:
            return
        generic = self.generic_id
        if not self.active_ingredient_ids:
            self.active_ingredient_ids = [(6, 0, generic.active_ingredient_ids.ids)]
        if not self.dosage_form_id and generic.dosage_form_id:
            self.dosage_form_id = generic.dosage_form_id
        if not self.atc_code and generic.atc_code:
            self.atc_code = generic.atc_code
        if not self.pharma_generic_name:
            self.pharma_generic_name = generic.name

    def action_view_equivalents(self):
        """Other products sharing this generic — the substitution shortlist (PRD 34)."""
        self.ensure_one()
        if not self.generic_id:
            raise UserError(_(
                "'%s' is not linked to a generic, so the system has no basis for "
                "calling anything equivalent to it.", self.display_name))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Equivalents of %s', self.display_name),
            'res_model': 'product.template',
            'view_mode': 'list,form',
            'domain': [('generic_id', '=', self.generic_id.id),
                       ('id', '!=', self.id)],
        }

    @api.constrains('is_controlled', 'controlled_schedule', 'requires_prescription')
    def _check_controlled_consistency(self):
        """A controlled drug is by definition prescription-only and scheduled.

        Enforced rather than merely defaulted: a controlled substance that is not
        flagged prescription-only would be sellable over the counter, which is the
        exact compliance failure this module exists to prevent.
        """
        for product in self:
            if product.is_controlled and product.pharma_type in NON_MEDICINE_TYPES:
                # Answer the real question first. Telling someone that their vitamin
                # "must also require a prescription" sends them to fix the wrong field.
                raise ValidationError(_(
                    "'%(name)s' is classified as %(type)s and cannot be a controlled "
                    "substance. Change its Product Type if this really is a controlled "
                    "medicine.",
                    name=product.display_name,
                    type=dict(PHARMA_TYPES)[product.pharma_type]))
            if product.is_controlled and not product.requires_prescription:
                raise ValidationError(_(
                    "'%s' is flagged as a controlled drug, so it must also require a "
                    "prescription.", product.display_name))
            if product.controlled_schedule and not product.is_controlled:
                raise ValidationError(_(
                    "'%s' has a controlled schedule but is not flagged as a "
                    "controlled drug.", product.display_name))

    @api.onchange('is_controlled')
    def _onchange_is_controlled(self):
        if self.is_controlled:
            self.requires_prescription = True

    @api.onchange('is_medicine')
    def _onchange_is_medicine(self):
        """Turn a product into a sellable, batch-tracked medicine in one tick.

        A pharmacy's whole reason to exist is selling medicines, so a medicine must be
        sellable at the counter (POS), sellable in bulk (Sales) AND batch/expiry
        controlled. Without lot tracking there is no recall, FEFO or expiry control;
        without available_in_pos it never reaches the till. All are defaults the user
        can still override (e.g. untick POS for a wholesale-only line).
        """
        if self.is_medicine:
            self.tracking = 'lot'
            # Only things that actually go off get expiry tracking. A blood-pressure
            # monitor is batch-tracked for recall but has no shelf life, and forcing one
            # on it makes every unit arrive expired.
            self.use_expiration_date = self.pharma_type not in NON_EXPIRING_TYPES
            self.sale_ok = True
            self.available_in_pos = True

    @api.model_create_multi
    def create(self, vals_list):
        """Same defaults for medicines created outside the form (bulk import, code).

        Only fills a field the caller left unspecified, so an explicit choice — e.g.
        available_in_pos=False for a wholesale-only SKU — is always respected.
        """
        for vals in vals_list:
            if vals.get('is_medicine'):
                vals.setdefault('sale_ok', True)
                vals.setdefault('available_in_pos', True)
                vals.setdefault('tracking', 'lot')
                if vals.get('pharma_type') not in NON_EXPIRING_TYPES:
                    vals.setdefault('use_expiration_date', True)
                    # ...and give it a shelf life, or product_expiry dates every batch
                    # today and the medicine is unsellable from the moment it arrives.
                    if vals.get('use_expiration_date', True) \
                            and not vals.get('expiration_time'):
                        vals['expiration_time'] = self._default_shelf_life_days()
        return super().create(vals_list)

    @api.model
    def _default_shelf_life_days(self):
        """Fallback shelf life for a new medicine, in days. Two years by default."""
        param = self.env['ir.config_parameter'].sudo().get_param(
            'sahal_pharmacy.default_shelf_life_days', 730)
        try:
            return max(int(param), 1)
        except (TypeError, ValueError):
            return 730

    def _search_display_name(self, operator, value):
        """Let staff find a medicine by generic or brand name, not only trade name.

        BRD 5.5 requires both generic and brand search at the point of sale.
        """
        domain = super()._search_display_name(operator, value)
        if value and isinstance(value, str):
            return ['|', '|', ('pharma_generic_name', operator, value),
                    ('brand_name', operator, value)] + domain
        return domain


class ProductProduct(models.Model):
    _inherit = 'product.product'

    def _search_display_name(self, operator, value):
        domain = super()._search_display_name(operator, value)
        if value and isinstance(value, str):
            return ['|', '|', ('pharma_generic_name', operator, value),
                    ('brand_name', operator, value)] + domain
        return domain
