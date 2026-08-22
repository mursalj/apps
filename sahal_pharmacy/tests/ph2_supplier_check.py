"""Supplier licensing, approval and returns (PRD 19, 20, 64).

    odoo shell -d <database> --no-http --logfile=/dev/null \
      < addons/sahal_pharmacy/tests/ph2_supplier_check.py

Rolls back at the end. Expect 21 PASS / 0 FAIL.
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
Partner = env['res.partner']
today = fields.Date.today()

# ---------------------------------------------------------------- licence status
valid = Partner.create({
    'name': 'PH2 Valid Supplier', 'is_pharma_supplier': True,
    'supplier_category': 'distributor', 'pharma_licence_no': 'LIC-1',
    'pharma_licence_expiry': today + timedelta(days=365)})
expiring = Partner.create({
    'name': 'PH2 Expiring Supplier', 'is_pharma_supplier': True,
    'pharma_licence_no': 'LIC-2', 'pharma_licence_expiry': today + timedelta(days=20)})
expired = Partner.create({
    'name': 'PH2 Expired Supplier', 'is_pharma_supplier': True,
    'pharma_licence_no': 'LIC-3', 'pharma_licence_expiry': today - timedelta(days=5)})
unlicensed = Partner.create({'name': 'PH2 No Licence', 'is_pharma_supplier': True})

check("a current licence reads valid", valid.pharma_licence_state == 'valid')
check("a licence inside the alert window reads expiring",
      expiring.pharma_licence_state == 'expiring', expiring.pharma_licence_state)
check("a lapsed licence reads expired", expired.pharma_licence_state == 'expired')
check("no licence is not the same as a valid one",
      unlicensed.pharma_licence_state == 'none')

# ---------------------------------------------------------------- approval
valid.action_approve_supplier()
check("approval is recorded with who and when",
      valid.is_approved_supplier and valid.supplier_approved_by_id == env.user
      and valid.supplier_approved_on)
try:
    with env.cr.savepoint():
        expired.action_approve_supplier()
    check("a supplier with a lapsed licence cannot be approved", False, "approved")
except Exception as e:
    check("a supplier with a lapsed licence cannot be approved",
          'expired on' in str(e), str(e)[:70])

technician = env['res.users'].create({
    'name': 'PH2 Technician', 'login': 'ph2_tech_check',
    'group_ids': [(6, 0, [env.ref('sahal_pharmacy.group_pharmacy_technician').id,
                          env.ref('base.group_user').id])]})
try:
    with env.cr.savepoint():
        unlicensed.with_user(technician).action_approve_supplier()
    check("a technician cannot approve a supplier", False, "approved")
except Exception as e:
    check("a technician cannot approve a supplier",
          'Pharmacy Manager' in str(e), str(e)[:70])

# ---------------------------------------------------------------- per-product approval
medicine = env['product.product'].create({
    'name': 'PH2 Amoxicillin 500', 'is_medicine': True, 'pharma_type': 'prescription',
    'requires_prescription': True, 'standard_price': 4.0})
env['product.supplierinfo'].create({
    'partner_id': valid.id, 'product_tmpl_id': medicine.product_tmpl_id.id,
    'price': 4.0, 'is_approved_for_product': True})
check("an approved vendor for the product shows on the product",
      valid in medicine.product_tmpl_id.approved_supplier_ids,
      medicine.product_tmpl_id.approved_supplier_ids.mapped('name'))

# ---------------------------------------------------------------- purchase guard
def po_for(partner, product=medicine):
    return env['purchase.order'].create({
        'partner_id': partner.id,
        'order_line': [(0, 0, {'product_id': product.id, 'product_qty': 10,
                               'price_unit': 4.0, 'name': product.name})]})

clean = po_for(valid)
check("an approved, licensed, product-approved vendor raises no warning",
      not clean.pharma_supplier_warning, clean.pharma_supplier_warning)

bad = po_for(unlicensed)
check("an unapproved vendor is flagged",
      'not an approved' in (bad.pharma_supplier_warning or ''),
      bad.pharma_supplier_warning)
check("the warning also names the missing vendor listing",
      'not listed as a vendor' in (bad.pharma_supplier_warning or ''),
      bad.pharma_supplier_warning)

lapsed_po = po_for(expired)
check("a lapsed licence is flagged on the order",
      'licence expired' in (lapsed_po.pharma_supplier_warning or ''),
      lapsed_po.pharma_supplier_warning)

# warn mode: confirmation proceeds but leaves a trace
env['ir.config_parameter'].sudo().set_param('sahal_pharmacy.supplier_enforcement', 'warn')
bad.button_confirm()
check("in warn mode the order confirms", bad.state in ('purchase', 'to approve'),
      bad.state)
messages = bad.message_ids.mapped('body')
check("in warn mode the reason is posted to the order",
      any('supplier warning' in (m or '').lower() for m in messages))

# block mode: it does not
env['ir.config_parameter'].sudo().set_param('sahal_pharmacy.supplier_enforcement', 'block')
blocked = po_for(unlicensed)
try:
    with env.cr.savepoint():
        blocked.button_confirm()
    check("in block mode the order is refused", False, "confirmed")
except Exception as e:
    check("in block mode the order is refused", 'cannot be confirmed' in str(e),
          str(e)[:70])

env['ir.config_parameter'].sudo().set_param('sahal_pharmacy.supplier_enforcement', 'off')
off = po_for(unlicensed)
off.button_confirm()
check("enforcement can be switched off entirely",
      off.state in ('purchase', 'to approve'), off.state)
env['ir.config_parameter'].sudo().set_param('sahal_pharmacy.supplier_enforcement', 'warn')

non_medicine = env['product.product'].create({'name': 'PH2 Paper Bags'})
plain = po_for(unlicensed, non_medicine)
check("a non-medicine purchase is never blocked by pharmacy rules",
      not plain.pharma_supplier_warning, plain.pharma_supplier_warning)

# ---------------------------------------------------------------- supplier returns
ret = env['pharmacy.supplier.return'].create({
    'partner_id': valid.id, 'reason': 'expired',
    'line_ids': [(0, 0, {'product_id': medicine.id, 'quantity': 5, 'unit_cost': 4.0})]})
check("a return gets a reference", ret.name.startswith('SRET/'), ret.name)
check("it totals what is going back",
      ret.total_quantity == 5 and ret.total_value == 20.0,
      (ret.total_quantity, ret.total_value))
try:
    with env.cr.savepoint():
        ret.with_user(technician).action_approve()
    check("a technician cannot approve a return", False, "approved")
except Exception as e:
    check("a technician cannot approve a return", 'pharmacist' in str(e), str(e)[:70])

ret.action_approve()
check("approval is attributed", ret.state == 'approved' and ret.approved_by_id == env.user)
ret.action_create_shipment()
check("shipping back creates a real picking to the supplier",
      bool(ret.picking_id) and ret.state == 'returned', ret.picking_id.name)
ret.action_create_credit_note()
check("claiming credit creates a real vendor credit note",
      ret.credit_note_id.move_type == 'in_refund' and ret.state == 'credited',
      ret.credit_note_id.move_type)
check("the credit note is for the value returned",
      abs(ret.credit_note_id.amount_untaxed - 20.0) < 0.01,
      ret.credit_note_id.amount_untaxed)
try:
    with env.cr.savepoint():
        ret.action_create_credit_note()
    check("a return cannot be credited twice", False, "second credit note created")
except Exception as e:
    check("a return cannot be credited twice", 'already exists' in str(e), str(e)[:70])

# ---------------------------------------------------------------- licence cron
moved = Partner._cron_pharma_licence_alert()
check("the licence cron reports the at-risk suppliers", moved >= 2, moved)

print("\nPH2 SUPPLIERS: %d PASS / %d FAIL" % (PASS, FAIL))
env.cr.rollback()
print("rolled back")
