"""Wholesale licensing, credit control and order quantities (PRD 46-51).

    odoo shell -d <database> --no-http --logfile=/dev/null \
      < addons/sahal_pharmacy/tests/ph3_wholesale_check.py

Rolls back at the end. Expect 22 PASS / 0 FAIL.
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
Sale = env['sale.order']
today = fields.Date.today()

medicine = env['product.product'].create({
    'name': 'PH3 Amoxicillin 500 box', 'is_medicine': True,
    'pharma_type': 'prescription', 'requires_prescription': True,
    'list_price': 10.0, 'wholesale_min_qty': 20, 'wholesale_max_qty': 500,
    'wholesale_multiple_qty': 20})
plain = env['product.product'].create({'name': 'PH3 Cotton Wool', 'list_price': 2.0})


def order_for(partner, product=medicine, qty=20):
    return Sale.create({
        'partner_id': partner.id,
        'order_line': [(0, 0, {'product_id': product.id, 'product_uom_qty': qty,
                               'price_unit': product.list_price})]})


good = Partner.create({
    'name': 'PH3 Licensed Pharmacy', 'is_wholesale_customer': True,
    'wholesale_category': 'pharmacy', 'wholesale_licence_no': 'WL-1',
    'wholesale_licence_expiry': today + timedelta(days=300),
    'is_approved_wholesale': True, 'credit_limit': 10000.0})

# ---------------------------------------------------------------- profile
check("a wholesale customer is flagged as one", good.is_wholesale_customer)
check("a current trading licence reads valid",
      good.wholesale_licence_state == 'valid', good.wholesale_licence_state)
check("available credit is the limit less what is owed",
      good.credit_available == 10000.0 - (good.credit or 0.0), good.credit_available)

clean = order_for(good)
check("an order for a licensed, in-credit customer has nothing against it",
      not clean.wholesale_block_reason, clean.wholesale_block_reason)
check("the order knows it is wholesale", clean.is_wholesale)
clean.action_confirm()
check("it confirms", clean.state == 'sale', clean.state)

# ---------------------------------------------------------------- licence
unapproved = Partner.create({
    'name': 'PH3 Unapproved Buyer', 'is_wholesale_customer': True,
    'wholesale_licence_no': 'WL-2',
    'wholesale_licence_expiry': today + timedelta(days=300)})
blocked = order_for(unapproved)
check("an unapproved buyer is blocked from medicines",
      'not approved to buy medicines' in (blocked.wholesale_block_reason or ''),
      blocked.wholesale_block_reason)
try:
    with env.cr.savepoint():
        blocked.action_confirm()
    check("confirmation is refused", False, "confirmed")
except Exception as e:
    check("confirmation is refused", 'cannot be confirmed' in str(e)
          or 'not approved to buy it wholesale' in str(e), str(e)[:80])
    # ...and the wording must point at the licence, not at the retail dispensing
    # screen: a wholesaler cannot fix a bulk order to a hospital with a prescription.
    check("the refusal tells a wholesaler what to actually fix",
          'prescription in the Pharmacy app' not in str(e), str(e)[:90])

expired = Partner.create({
    'name': 'PH3 Lapsed Buyer', 'is_wholesale_customer': True,
    'is_approved_wholesale': True, 'wholesale_licence_no': 'WL-3',
    'wholesale_licence_expiry': today - timedelta(days=1)})
lapsed_order = order_for(expired)
check("a lapsed trading licence blocks the sale",
      'licence expired' in (lapsed_order.wholesale_block_reason or ''),
      lapsed_order.wholesale_block_reason)

non_medicine_order = order_for(expired, plain, qty=5)
check("a non-medicine sale is not blocked by the medicine licence",
      'licence expired' not in (non_medicine_order.wholesale_block_reason or ''),
      non_medicine_order.wholesale_block_reason)

# ---------------------------------------------------------------- credit
over = Partner.create({
    'name': 'PH3 Over Limit', 'is_wholesale_customer': True,
    'is_approved_wholesale': True, 'wholesale_licence_no': 'WL-4',
    'wholesale_licence_expiry': today + timedelta(days=300), 'credit_limit': 100.0})
big = order_for(over, medicine, qty=100)   # 100 x 10 = 1,000 against a 100 limit
check("an order beyond the credit limit is flagged",
      'credit limit' in (big.wholesale_block_reason or ''), big.wholesale_block_reason)

held = Partner.create({
    'name': 'PH3 On Hold', 'is_wholesale_customer': True,
    'is_approved_wholesale': True, 'wholesale_licence_no': 'WL-5',
    'wholesale_licence_expiry': today + timedelta(days=300),
    'credit_limit': 50000.0, 'credit_hold_reason': 'Cheque returned unpaid'})
held.action_place_on_credit_hold()
check("a credit hold is recorded on the account", held.credit_hold)
hold_order = order_for(held)
check("a credit hold blocks the order regardless of the limit",
      'credit hold' in (hold_order.wholesale_block_reason or ''),
      hold_order.wholesale_block_reason)

technician = env['res.users'].create({
    'name': 'PH3 Technician', 'login': 'ph3_tech_check',
    'group_ids': [(6, 0, [env.ref('sahal_pharmacy.group_pharmacy_technician').id,
                          env.ref('base.group_user').id])]})
try:
    with env.cr.savepoint():
        held.with_user(technician).action_release_credit_hold()
    check("a technician cannot release a credit hold", False, "released")
except Exception as e:
    check("a technician cannot release a credit hold",
          'Pharmacy Manager' in str(e), str(e)[:70])
held.action_release_credit_hold()
check("a manager can release it", not held.credit_hold)

# ---------------------------------------------------------------- quantities
small = order_for(good, medicine, qty=5)
check("below the minimum order quantity is refused",
      'minimum order' in (small.wholesale_block_reason or ''),
      small.wholesale_block_reason)
huge = order_for(good, medicine, qty=600)
check("above the maximum order quantity is refused",
      'limited to' in (huge.wholesale_block_reason or ''), huge.wholesale_block_reason)
odd = order_for(good, medicine, qty=30)
check("a quantity that is not a whole case is refused",
      'multiples of' in (odd.wholesale_block_reason or ''), odd.wholesale_block_reason)
whole_case = order_for(good, medicine, qty=40)
check("two whole cases pass", not whole_case.wholesale_block_reason,
      whole_case.wholesale_block_reason)

# ---------------------------------------------------------------- override
try:
    with env.cr.savepoint():
        odd.with_user(technician).action_confirm_wholesale_override()
    check("a technician cannot override the block", False, "overrode")
except Exception as e:
    check("a technician cannot override the block", 'Pharmacy Manager' in str(e),
          str(e)[:70])

odd.action_confirm_wholesale_override()
check("a manager can override", odd.state == 'sale', odd.state)
check("the override is on the record",
      any('overridden by' in (m or '').lower() for m in odd.message_ids.mapped('body')))

# ---------------------------------------------------------------- retail untouched
walk_in = Partner.create({'name': 'PH3 Walk-in Customer'})
retail = order_for(walk_in, plain, qty=1)
check("a retail sale is not subject to wholesale rules",
      not retail.is_wholesale and not retail.wholesale_block_reason)

# ---------------------------------------------------------------- pricelists
for xmlid in ('pricelist_wholesale', 'pricelist_distributor', 'pricelist_hospital',
              'pricelist_government', 'pricelist_ngo'):
    if not env.ref('sahal_pharmacy.%s' % xmlid, raise_if_not_found=False):
        check("the PRD's price tiers exist as pricelists", False, xmlid)
        break
else:
    check("the PRD's price tiers exist as pricelists", True)

print("\nPH3 WHOLESALE: %d PASS / %d FAIL" % (PASS, FAIL))
env.cr.rollback()
print("rolled back")
