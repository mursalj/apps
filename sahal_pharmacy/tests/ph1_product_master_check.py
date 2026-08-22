"""Product classification and pharmaceutical master (PRD 7-10).

    odoo shell -d <database> --no-http --logfile=/dev/null \
      < addons/sahal_pharmacy/tests/ph1_product_master_check.py

Rolls back at the end; safe on production. Expect 22 PASS / 0 FAIL.
"""
PASS = FAIL = 0


def check(label, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % label)
    else:
        FAIL += 1
        print("  FAIL  %s   %s" % (label, detail))


Product = env['product.template']
Generic = env['pharmacy.generic']
Ingredient = env['pharmacy.active.ingredient']

# ---------------------------------------------------------------- vocabulary
tablet = env.ref('sahal_pharmacy.dosage_tablet', raise_if_not_found=False)
check("dosage forms are seeded as records", bool(tablet) and tablet.name == 'Tablet')
check("the PRD's full dosage list is present",
      env['pharmacy.dosage.form'].search_count([]) >= 26,
      env['pharmacy.dosage.form'].search_count([]))
check("routes are seeded", bool(env.ref('sahal_pharmacy.route_oral', raise_if_not_found=False)))
check("an administrator can add a dosage form without code",
      bool(env['pharmacy.dosage.form'].create({'name': 'PH1 Pessary', 'code': 'PES'})))

amox = Ingredient.create({'name': 'PH1 Amoxicillin', 'atc_code': 'J01CA04',
                          'is_antibiotic': True, 'allergy_alias': 'penicillin'})
check("an active ingredient is a record, not a word", bool(amox.id))

generic = Generic.create({
    'name': 'PH1 Amoxicillin 500 mg capsule', 'strength': '500 mg',
    'active_ingredient_ids': [(6, 0, amox.ids)],
    'dosage_form_id': env.ref('sahal_pharmacy.dosage_capsule').id})
try:
    with env.cr.savepoint():
        Generic.create({'name': 'PH1 Empty Generic'})
    check("a generic without an ingredient is refused", False, "created")
except Exception as e:
    check("a generic without an ingredient is refused",
          'active ingredient' in str(e), str(e)[:60])

# ---------------------------------------------------------------- classification
rx = Product.create({
    'name': 'PH1 Amoxil 500', 'is_medicine': True, 'pharma_type': 'prescription',
    'requires_prescription': True, 'generic_id': generic.id,
    'active_ingredient_ids': [(6, 0, amox.ids)],
    'strength_value': 500, 'strength_uom': 'mg'})
check("a prescription medicine is classified", rx.pharma_type == 'prescription')
check("strength renders from value and unit", rx.strength == '500 mg', rx.strength)
check("it is not over the counter", not rx.is_otc)

vitamin = Product.create({
    'name': 'PH1 Vitamin C 1000', 'is_medicine': True, 'pharma_type': 'supplement',
    'strength_value': 1000, 'strength_uom': 'mg'})
check("a supplement is a pharmacy product but not a drug",
      vitamin.is_medicine and vitamin.pharma_type == 'supplement')
check("a supplement is over the counter", vitamin.is_otc)

try:
    with env.cr.savepoint():
        vitamin.requires_prescription = True
    check("a supplement cannot require a prescription", False, "accepted")
except Exception as e:
    check("a supplement cannot require a prescription",
          'cannot require a prescription' in str(e), str(e)[:70])

try:
    with env.cr.savepoint():
        vitamin.is_controlled = True
    check("a supplement cannot be a controlled substance", False, "accepted")
except Exception as e:
    check("a supplement cannot be a controlled substance",
          'controlled substance' in str(e), str(e)[:70])

try:
    with env.cr.savepoint():
        Product.create({'name': 'PH1 Bad Rx', 'is_medicine': True,
                        'pharma_type': 'prescription', 'requires_prescription': False})
    check("a prescription type must require a prescription", False, "accepted")
except Exception as e:
    check("a prescription type must require a prescription",
          'must require' in str(e), str(e)[:70])

device = Product.create({'name': 'PH1 BP Monitor', 'is_medicine': True,
                         'pharma_type': 'device'})
check("a device is stocked without pharmacology",
      device.pharma_type == 'device' and not device.requires_prescription)

# ---------------------------------------------------------------- equivalents
brand_b = Product.create({
    'name': 'PH1 Moxatag 500', 'is_medicine': True, 'pharma_type': 'prescription',
    'requires_prescription': True, 'generic_id': generic.id})
action = rx.action_view_equivalents()
equivalents = Product.search(action['domain'])
check("equivalents are found through the generic", brand_b in equivalents,
      equivalents.mapped('name'))
check("a product is not its own equivalent", rx not in equivalents)
check("the generic knows its brands", generic.product_count == 2, generic.product_count)

orphan = Product.create({'name': 'PH1 No Generic', 'is_medicine': True,
                         'pharma_type': 'otc'})
try:
    with env.cr.savepoint():
        orphan.action_view_equivalents()
    check("a product with no generic refuses to guess equivalents", False, "returned")
except Exception as e:
    check("a product with no generic refuses to guess equivalents",
          'not linked to a generic' in str(e), str(e)[:70])

# ---------------------------------------------------------------- handling flags
cold = Product.create({
    'name': 'PH1 Insulin', 'is_medicine': True, 'pharma_type': 'prescription',
    'requires_prescription': True, 'is_high_alert': True,
    'requires_cold_storage': True, 'temperature_min': 2, 'temperature_max': 8})
check("cold-chain range is recorded", cold.temperature_min == 2 and cold.temperature_max == 8)
try:
    with env.cr.savepoint():
        cold.temperature_min = 30
    check("an inverted temperature range is refused", False, "accepted")
except Exception as e:
    check("an inverted temperature range is refused",
          'above the maximum' in str(e), str(e)[:70])

# ---------------------------------------------------------------- the backfill logic
# The migration classifies from the flags. Run its exact CASE over probe rows and
# confirm a prescription medicine can never come out as over-the-counter.
probe_rx = Product.create({'name': 'PH1 Legacy Rx', 'is_medicine': True,
                           'pharma_type': 'prescription', 'requires_prescription': True})
probe_otc = Product.create({'name': 'PH1 Legacy OTC', 'is_medicine': True,
                            'pharma_type': 'otc'})
probe_plain = Product.create({'name': 'PH1 Legacy Plain'})
env.flush_all()
env.cr.execute("""
    SELECT id, CASE
        WHEN COALESCE(is_medicine, false) AND COALESCE(requires_prescription, false)
            THEN 'prescription'
        WHEN COALESCE(is_medicine, false) THEN 'otc'
        ELSE 'other' END
      FROM product_template WHERE id IN %s
""", (tuple([probe_rx.id, probe_otc.id, probe_plain.id]),))
classified = dict(env.cr.fetchall())
check("backfill: a prescription-only medicine becomes 'prescription'",
      classified[probe_rx.id] == 'prescription', classified.get(probe_rx.id))
check("backfill: any other medicine becomes 'otc'",
      classified[probe_otc.id] == 'otc', classified.get(probe_otc.id))
check("backfill: a non-pharmacy product becomes 'other'",
      classified[probe_plain.id] == 'other', classified.get(probe_plain.id))

# ---------------------------------------------------------------- search
found = Product.search([('active_ingredient_ids', 'in', amox.ids)])
check("products can be found by what they contain", rx in found, found.mapped('name'))

print("\nPH1 PRODUCT MASTER: %d PASS / %d FAIL" % (PASS, FAIL))
env.cr.rollback()
print("rolled back")
