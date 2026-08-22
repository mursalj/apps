"""Insurance claims and the copay split (PRD 44).

    odoo shell -d <database> --no-http --logfile=/dev/null \
      < addons/sahal_pharmacy/tests/ph10_insurance_check.py

Rolls back at the end. Expect 16 PASS / 0 FAIL.
"""
from datetime import timedelta
from odoo import fields

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
Claim = env['pharmacy.insurance.claim']
today = fields.Date.today()

insurer = env['res.partner'].create({'name': 'PH10 Health Cover', 'is_insurer': True})
patient = env['res.partner'].create({
    'name': 'PH10 Patient', 'is_patient': True, 'insurer_id': insurer.id,
    'insurance_member_no': 'POL-10', 'insurance_coverage_pct': 80.0,
    'insurance_valid_until': today + timedelta(days=200)})
lapsed_patient = env['res.partner'].create({
    'name': 'PH10 Lapsed Cover', 'is_patient': True, 'insurer_id': insurer.id,
    'insurance_member_no': 'POL-OLD', 'insurance_coverage_pct': 50.0,
    'insurance_valid_until': today - timedelta(days=1)})

# ---------------------------------------------------------------- the split
claim = Claim.create({
    'patient_id': patient.id, 'insurer_id': insurer.id, 'policy_no': 'POL-10',
    'coverage_pct': 80.0, 'total_amount': 100.0})
check("a claim gets a reference", claim.name.startswith('CLM/'), claim.name)
check("the insurer's share is computed", claim.covered_amount == 80.0,
      claim.covered_amount)
check("the patient's copay is the remainder", claim.patient_amount == 20.0,
      claim.patient_amount)

try:
    with env.cr.savepoint():
        Claim.create({'patient_id': patient.id, 'insurer_id': insurer.id,
                      'coverage_pct': 140.0, 'total_amount': 100.0})
    check("coverage above 100% is refused", False, "created")
except Exception as e:
    check("coverage above 100% is refused", 'between 0 and 100' in str(e), str(e)[:60])

try:
    with env.cr.savepoint():
        over = Claim.create({'patient_id': patient.id, 'insurer_id': insurer.id,
                             'coverage_pct': 50.0, 'total_amount': 100.0})
        over.covered_amount = 200.0
        over.flush_recordset()
    check("the insurer cannot be claimed more than the bill", False, "accepted")
except Exception as e:
    check("the insurer cannot be claimed more than the bill",
          'more than the bill' in str(e), str(e)[:60])

# ---------------------------------------------------------------- workflow
try:
    with env.cr.savepoint():
        claim.action_invoice_insurer()
    check("an unapproved claim cannot be invoiced", False, "invoiced")
except Exception as e:
    check("an unapproved claim cannot be invoiced", 'once the claim is approved' in str(e),
          str(e)[:70])

claim.action_submit()
check("submitting records the date", claim.state == 'submitted' and claim.submitted_on)

rejected = Claim.create({
    'patient_id': patient.id, 'insurer_id': insurer.id, 'coverage_pct': 80.0,
    'total_amount': 50.0})
rejected.action_submit()
try:
    with env.cr.savepoint():
        rejected.action_reject()
    check("a rejection must say why", False, "rejected with no reason")
except Exception as e:
    check("a rejection must say why", 'Record why' in str(e), str(e)[:60])
rejected.rejection_reason = 'Policy excludes this medicine'
rejected.action_reject()
check("a rejection is recorded with its reason and date",
      rejected.state == 'rejected' and rejected.response_on)

claim.action_approve()
check("approval is recorded", claim.state == 'approved' and claim.response_on)
claim.action_invoice_insurer()
check("the insurer is invoiced for their share only",
      claim.invoice_id.move_type == 'out_invoice'
      and abs(claim.invoice_id.amount_untaxed - 80.0) < 0.01,
      claim.invoice_id.amount_untaxed)
check("the invoice goes to the insurer, not the patient",
      claim.invoice_id.partner_id == insurer, claim.invoice_id.partner_id.name)
try:
    with env.cr.savepoint():
        claim.action_invoice_insurer()
    check("a claim cannot be invoiced twice", False, "second invoice")
except Exception as e:
    check("a claim cannot be invoiced twice", 'already exists' in str(e), str(e)[:60])

# ---------------------------------------------------------------- from a dispensing
medicine = env['product.product'].create({
    'name': 'PH10 Amoxicillin', 'is_medicine': True, 'pharma_type': 'prescription',
    'requires_prescription': True, 'list_price': 50.0, 'is_storable': True})
doctor = env['res.partner'].create({'name': 'PH10 Prescriber', 'is_doctor': True})
rx = env['pharmacy.prescription'].create({
    'patient_id': patient.id, 'doctor_id': doctor.id,
    'line_ids': [(0, 0, {'product_id': medicine.id, 'qty_prescribed': 2})]})
rx.action_verify()
dispense = env['pharmacy.dispense'].create({
    'prescription_id': rx.id, 'pharmacist_id': env.user.id,
    'line_ids': [(0, 0, {'product_id': medicine.id, 'quantity': 2})]})
check("an insured patient is recognised at the counter", dispense.patient_is_insured)
try:
    with env.cr.savepoint():
        dispense.action_create_insurance_claim()
    check("a claim cannot precede the dispensing itself", False, "claimed")
except Exception as e:
    check("a claim cannot precede the dispensing itself",
          'Confirm the dispensing first' in str(e), str(e)[:70])

dispense.action_confirm()
dispense.action_create_insurance_claim()
raised = dispense.insurance_claim_ids
check("the claim is raised from what was actually supplied",
      len(raised) == 1 and raised.total_amount == dispense.sale_order_id.amount_total,
      (len(raised), raised.total_amount))
check("it inherits the patient's cover", raised.coverage_pct == 80.0
      and raised.policy_no == 'POL-10', (raised.coverage_pct, raised.policy_no))
try:
    with env.cr.savepoint():
        dispense.action_create_insurance_claim()
    check("one dispensing cannot be claimed twice", False, "claimed again")
except Exception as e:
    check("one dispensing cannot be claimed twice", 'already exists' in str(e),
          str(e)[:60])

# lapsed cover
rx2 = env['pharmacy.prescription'].create({
    'patient_id': lapsed_patient.id, 'doctor_id': doctor.id,
    'line_ids': [(0, 0, {'product_id': medicine.id, 'qty_prescribed': 1})]})
rx2.action_verify()
dispense2 = env['pharmacy.dispense'].create({
    'prescription_id': rx2.id, 'pharmacist_id': env.user.id,
    'line_ids': [(0, 0, {'product_id': medicine.id, 'quantity': 1})]})
dispense2.action_confirm()
try:
    with env.cr.savepoint():
        dispense2.action_create_insurance_claim()
    check("expired cover cannot be claimed against", False, "claimed")
except Exception as e:
    check("expired cover cannot be claimed against", 'cover expired' in str(e),
          str(e)[:70])

print("\nPH10 INSURANCE: %d PASS / %d FAIL" % (PASS, FAIL))
env.cr.rollback()
print("rolled back")
