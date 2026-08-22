"""Allergy screening, drug interactions and substitution (PRD 32, 33, 34).

    odoo shell -d <database> --no-http --logfile=/dev/null \
      < addons/sahal_pharmacy/tests/ph6_clinical_check.py

Rolls back at the end. Expect 18 PASS / 0 FAIL.
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


env.user.group_ids = [(4, env.ref('sahal_pharmacy.group_pharmacy_pharmacist').id)]
Ingredient = env['pharmacy.active.ingredient']
Product = env['product.product']

amoxicillin = Ingredient.create({
    'name': 'PH6 Amoxicillin', 'allergy_alias': 'penicillin, beta-lactam',
    'is_antibiotic': True})
warfarin = Ingredient.create({'name': 'PH6 Warfarin'})
aspirin = Ingredient.create({'name': 'PH6 Aspirin'})
paracetamol = Ingredient.create({'name': 'PH6 Paracetamol'})


def medicine(name, ingredients, rx=True):
    return Product.create({
        'name': name, 'is_medicine': True,
        'pharma_type': 'prescription' if rx else 'otc',
        'requires_prescription': rx,
        'active_ingredient_ids': [(6, 0, [i.id for i in ingredients])]})


amoxil = medicine('PH6 Amoxil 500', [amoxicillin])
warf = medicine('PH6 Warfarin 5mg', [warfarin])
asp = medicine('PH6 Aspirin 300', [aspirin], rx=False)
para = medicine('PH6 Paracetamol 500', [paracetamol], rx=False)

interaction = env['pharmacy.drug.interaction'].create({
    'ingredient_a_id': warfarin.id, 'ingredient_b_id': aspirin.id,
    'severity': 'critical', 'description': 'Greatly increased bleeding risk.',
    'recommended_action': 'Do not dispense together without prescriber contact.'})
check("an interaction pair is recorded", bool(interaction.id))
try:
    with env.cr.savepoint():
        env['pharmacy.drug.interaction'].create({
            'ingredient_a_id': warfarin.id, 'ingredient_b_id': warfarin.id,
            'severity': 'low', 'description': 'x'})
    check("an ingredient cannot interact with itself", False, "created")
except Exception as e:
    check("an ingredient cannot interact with itself", 'itself' in str(e), str(e)[:60])

# ---------------------------------------------------------------- allergy, coded
patient = env['res.partner'].create({
    'name': 'PH6 Patient Coded', 'is_patient': True,
    'allergy_ingredient_ids': [(6, 0, [amoxicillin.id])]})
findings = patient.pharmacy_screen_products(amoxil)
check("a coded allergy is caught by ingredient, not by brand name",
      any(f['kind'] == 'allergy' for f in findings), findings)
check("a coded allergy is critical",
      findings and findings[0]['severity'] == 'critical', findings)
check("an unrelated medicine raises nothing",
      not patient.pharmacy_screen_products(para))

# ---------------------------------------------------------------- allergy, free text
noted = env['res.partner'].create({
    'name': 'PH6 Patient Noted', 'is_patient': True,
    'allergies': 'Penicillin - rash as a child'})
noted_findings = noted.pharmacy_screen_products(amoxil)
check("a free-text allergy still matches through the ingredient alias",
      any(f['kind'] == 'allergy' for f in noted_findings), noted_findings)
check("the free-text match is flagged for confirmation rather than treated as certain",
      noted_findings and noted_findings[0]['severity'] == 'high',
      noted_findings)

# ---------------------------------------------------------------- interactions
plain = env['res.partner'].create({'name': 'PH6 Patient Plain', 'is_patient': True})
pair = plain.pharmacy_screen_products(warf | asp)
check("an interaction between two dispensed medicines is found",
      any(f['kind'] == 'interaction' for f in pair), pair)
check("the finding carries the severity and the action",
      any('bleeding' in f['message'] for f in pair), pair)
check("the pair is found whichever way round it is listed",
      bool(plain.pharmacy_screen_products(asp | warf)))
check("one medicine alone raises no interaction",
      not plain.pharmacy_screen_products(warf))

# ---------------------------------------------------------------- the gate
doctor = env['res.partner'].create({
    'name': 'PH6 Prescriber', 'is_doctor': True, 'medical_license_no': 'MD-PH6'})
rx = env['pharmacy.prescription'].create({
    'patient_id': patient.id, 'doctor_id': doctor.id,
    'line_ids': [(0, 0, {'product_id': amoxil.id, 'qty_prescribed': 10})]})
check("the prescription surfaces the warning",
      'allergic' in (rx.clinical_warning or ''), rx.clinical_warning)
check("and its severity", rx.clinical_severity == 'critical', rx.clinical_severity)

try:
    with env.cr.savepoint():
        rx.action_verify()
    check("a critical warning blocks verification until reviewed", False, "verified")
except Exception as e:
    check("a critical warning blocks verification until reviewed",
          'not been reviewed' in str(e), str(e)[:70])

technician = env['res.users'].create({
    'name': 'PH6 Technician', 'login': 'ph6_tech_check',
    'group_ids': [(6, 0, [env.ref('sahal_pharmacy.group_pharmacy_technician').id,
                          env.ref('base.group_user').id])]})
try:
    with env.cr.savepoint():
        rx.with_user(technician).action_acknowledge_clinical()
    check("a technician cannot sign off a clinical warning", False, "signed off")
except Exception as e:
    check("a technician cannot sign off a clinical warning",
          'pharmacist' in str(e), str(e)[:70])

rx.clinical_ack_note = 'Patient tolerated amoxicillin last month; prescriber contacted.'
rx.action_acknowledge_clinical()
check("a pharmacist can sign it off, by name",
      rx.clinical_ack_by_id == env.user and rx.clinical_ack_on)
rx.action_verify()
check("the script verifies once the decision is recorded", rx.state == 'verified')
check("the decision is in the record's history",
      any('reviewed' in (m or '').lower() for m in rx.message_ids.mapped('body')))

# ---------------------------------------------------------------- substitution
generic = env['pharmacy.generic'].create({
    'name': 'PH6 Amoxicillin 500', 'active_ingredient_ids': [(6, 0, [amoxicillin.id])]})
brand_a = medicine('PH6 Brand A', [amoxicillin])
brand_b = medicine('PH6 Brand B', [amoxicillin])
(brand_a | brand_b).mapped('product_tmpl_id').write({'generic_id': generic.id})

sub = env['pharmacy.substitution'].create({
    'prescription_id': rx.id, 'original_product_id': brand_a.id,
    'substitute_product_id': brand_b.id, 'reason': 'out_of_stock', 'quantity': 10})
check("a substitution between two brands of one generic is marked equivalent",
      sub.is_equivalent)
unequal = env['pharmacy.substitution'].create({
    'prescription_id': rx.id, 'original_product_id': brand_a.id,
    'substitute_product_id': para.id, 'reason': 'other'})
check("a substitution across different generics is NOT called equivalent",
      not unequal.is_equivalent)
check("every substitution is written to the prescription's history",
      sum('Substitution' in (m or '') for m in rx.message_ids.mapped('body')) >= 2)

try:
    with env.cr.savepoint():
        env['pharmacy.substitution'].with_user(technician).create({
            'prescription_id': rx.id, 'original_product_id': brand_a.id,
            'substitute_product_id': brand_b.id, 'reason': 'generic'})
    check("a technician cannot substitute a prescribed medicine", False, "created")
except Exception as e:
    check("a technician cannot substitute a prescribed medicine",
          'pharmacist' in str(e).lower(), str(e)[:70])

print("\nPH6 CLINICAL SAFETY: %d PASS / %d FAIL" % (PASS, FAIL))
env.cr.rollback()
print("rolled back")
