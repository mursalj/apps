"""Pharmacy profile, manufacturers, regulatory documents and overrides (PRD 5, 70-73).

    odoo shell -d <database> --no-http --logfile=/dev/null \
      < addons/sahal_pharmacy/tests/ph9_compliance_check.py

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


env.user.group_ids = [(4, env.ref('sahal_pharmacy.group_pharmacy_manager').id)]
today = fields.Date.today()

# ---------------------------------------------------------------- pharmacy profile
branch = env['pharmacy.profile'].create({
    'name': 'PH9 Main Branch', 'legal_name': 'PH9 Pharmacy Ltd', 'branch_code': 'MAIN',
    'licence_no': 'PH-1234', 'licence_authority': 'Ministry of Health',
    'licence_expiry': today + timedelta(days=200),
    'pharmacist_in_charge_id': env.user.id, 'city': 'Hargeisa'})
check("a pharmacy can be described, not just a company", bool(branch.id))
check("a current licence reads valid", branch.licence_state == 'valid',
      branch.licence_state)
lapsed = env['pharmacy.profile'].create({
    'name': 'PH9 Lapsed Branch', 'licence_no': 'PH-OLD',
    'licence_expiry': today - timedelta(days=1)})
check("a lapsed pharmacy licence is visible as expired",
      lapsed.licence_state == 'expired', lapsed.licence_state)
soon = env['pharmacy.profile'].create({
    'name': 'PH9 Expiring Branch', 'licence_expiry': today + timedelta(days=15)})
check("one about to lapse is flagged before it does",
      soon.licence_state == 'expiring', soon.licence_state)

# ---------------------------------------------------------------- manufacturers
maker = env['pharmacy.manufacturer'].create({
    'name': 'PH9 Generics Ltd', 'registration_no': 'MFG-9',
    'gmp_certificate_no': 'GMP-9', 'gmp_expiry': today + timedelta(days=365)})
check("a manufacturer is a record with a registration", bool(maker.id))
check("it starts unapproved", not maker.is_approved)

technician = env['res.users'].create({
    'name': 'PH9 Technician', 'login': 'ph9_tech_check',
    'group_ids': [(6, 0, [env.ref('sahal_pharmacy.group_pharmacy_technician').id,
                          env.ref('base.group_user').id])]})
try:
    with env.cr.savepoint():
        maker.with_user(technician).action_approve()
    check("a technician cannot approve a manufacturer", False, "approved")
except Exception as e:
    check("a technician cannot approve a manufacturer",
          'Pharmacy Manager' in str(e), str(e)[:70])

maker.action_approve()
check("approval is attributed", maker.is_approved and maker.approved_by_id == env.user)

medicine = env['product.product'].create({
    'name': 'PH9 Amoxicillin', 'is_medicine': True, 'pharma_type': 'otc',
    'pharma_manufacturer_id': maker.id})
check("a product can name its licensed manufacturer",
      medicine.product_tmpl_id.pharma_manufacturer_id == maker)
maker.invalidate_recordset()
check("the manufacturer counts its products", maker.product_count == 1,
      maker.product_count)

# ---------------------------------------------------------------- documents
Document = env['pharmacy.document']
permit = Document.create({
    'name': 'PH9 Import Permit', 'document_type': 'import_permit',
    'reference': 'IMP-9', 'issuing_authority': 'Customs',
    'issue_date': today - timedelta(days=100),
    'expiry_date': today + timedelta(days=10), 'profile_id': branch.id})
check("a document tracks its expiry", permit.state == 'expiring', permit.state)
dead = Document.create({
    'name': 'PH9 Old Registration', 'document_type': 'registration',
    'expiry_date': today - timedelta(days=30)})
check("an expired document says so", dead.state == 'expired')
forever = Document.create({'name': 'PH9 Constitution', 'document_type': 'other'})
check("a document with no expiry is not treated as expiring",
      forever.state == 'none', forever.state)

try:
    with env.cr.savepoint():
        Document.create({
            'name': 'PH9 Backwards', 'document_type': 'other',
            'issue_date': today, 'expiry_date': today - timedelta(days=5)})
    check("a document cannot expire before it was issued", False, "created")
except Exception as e:
    check("a document cannot expire before it was issued",
          'before it was issued' in str(e), str(e)[:70])

flagged = Document._cron_document_expiry_alert()
check("the document cron reports what is lapsing", flagged >= 2, flagged)

# ---------------------------------------------------------------- override log
Override = env['pharmacy.override']
before = Override.search_count([])

# A wholesale block overridden by a manager must land in the log.
# A licensed, approved buyer who is simply over their credit limit: the commercial
# block a manager MAY override.
customer = env['res.partner'].create({
    'name': 'PH9 Wholesale Buyer', 'is_wholesale_customer': True,
    'is_approved_wholesale': True, 'wholesale_licence_no': 'WL-9',
    'wholesale_licence_expiry': today + timedelta(days=200), 'credit_limit': 10.0})
rx_medicine = env['product.product'].create({
    'name': 'PH9 Rx Item', 'is_medicine': True, 'pharma_type': 'prescription',
    'requires_prescription': True, 'list_price': 20.0})
order = env['sale.order'].create({
    'partner_id': customer.id,
    'order_line': [(0, 0, {'product_id': rx_medicine.id, 'product_uom_qty': 5,
                           'price_unit': 20.0})]})
order.action_confirm_wholesale_override()
check("a wholesale override is written to the central log",
      Override.search_count([]) == before + 1, Override.search_count([]))
logged = Override.search([], order='id desc', limit=1)
check("the log says what kind of override it was", logged.kind == 'wholesale',
      logged.kind)
check("it names who authorised it and why",
      logged.user_id == env.user and logged.reason, (logged.user_id.name, logged.reason))
check("it points back at the document",
      logged.res_model == 'sale.order' and logged.res_id == order.id,
      (logged.res_model, logged.res_id))

# ...but a manager may NOT override the buyer's licence, and is told so plainly.
unlicensed = env['res.partner'].create({
    'name': 'PH9 Unlicensed Buyer', 'is_wholesale_customer': True,
    'is_approved_wholesale': False})
blocked_order = env['sale.order'].create({
    'partner_id': unlicensed.id,
    'order_line': [(0, 0, {'product_id': rx_medicine.id, 'product_uom_qty': 1,
                           'price_unit': 20.0})]})
try:
    with env.cr.savepoint():
        blocked_order.action_confirm_wholesale_override()
    check("a manager cannot override the buyer's licence", False, "overrode")
except Exception as e:
    check("a manager cannot override the buyer's licence",
          'cannot be overridden' in str(e), str(e)[:70])

print("\nPH9 COMPLIANCE: %d PASS / %d FAIL" % (PASS, FAIL))
env.cr.rollback()
print("rolled back")
