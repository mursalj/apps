"""Customer returns: batch, condition and the restock decision (PRD 45).

    odoo shell -d <database> --no-http --logfile=/dev/null \
      < addons/sahal_pharmacy/tests/ph7_customer_return_check.py

Rolls back at the end. Expect 17 PASS / 0 FAIL.
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
Return = env['pharmacy.return']

customer = env['res.partner'].create({'name': 'PH7 Customer', 'is_patient': True})
medicine = env['product.product'].create({
    'name': 'PH7 Amoxicillin 500', 'is_medicine': True, 'pharma_type': 'otc',
    'tracking': 'lot', 'use_expiration_date': True, 'list_price': 12.0,
    'is_storable': True})
good_lot = env['stock.lot'].create({
    'name': 'PH7-GOOD', 'product_id': medicine.id,
    'expiration_date': fields.Datetime.now() + timedelta(days=200)})
dead_lot = env['stock.lot'].create({
    'name': 'PH7-DEAD', 'product_id': medicine.id,
    'expiration_date': fields.Datetime.now() - timedelta(days=2)})


def make_return(condition='sealed', restock=False, lot=None, refund=True):
    return Return.create({
        'partner_id': customer.id, 'reason': 'changed_mind',
        'line_ids': [(0, 0, {
            'product_id': medicine.id, 'lot_id': (lot or good_lot).id,
            'quantity': 2, 'unit_price': 12.0, 'condition': condition,
            'restock': restock, 'refund': refund})]})


# ---------------------------------------------------------------- the core rule
sealed = make_return('sealed', restock=True)
check("a return gets a reference", sealed.name.startswith('CRET/'), sealed.name)
check("a sealed, in-date pack may be restocked", sealed.line_ids.restock)

for condition in ('opened', 'damaged', 'expired', 'unknown'):
    try:
        with env.cr.savepoint():
            make_return(condition, restock=True)
        check("%s stock cannot be put back on sale" % condition, False, "accepted")
        break
    except Exception as e:
        if 'cannot go back on sale' not in str(e):
            check("%s stock cannot be put back on sale" % condition, False, str(e)[:60])
            break
else:
    check("opened, damaged, expired or unknown stock cannot be put back on sale", True)

try:
    with env.cr.savepoint():
        make_return('sealed', restock=True, lot=dead_lot)
    check("a sealed pack from an EXPIRED batch still cannot be restocked", False,
          "accepted")
except Exception as e:
    check("a sealed pack from an EXPIRED batch still cannot be restocked",
          'expired on' in str(e), str(e)[:70])

default_off = make_return('sealed', restock=False)
check("restocking is off by default", not default_off.line_ids.restock)

# ---------------------------------------------------------------- refund is separate
no_refund = make_return('opened', restock=False, refund=False)
check("medicine can be accepted back without a refund",
      no_refund.refund_amount == 0.0, no_refund.refund_amount)
refunded = make_return('opened', restock=False, refund=True)
check("and refunded without being restocked",
      refunded.refund_amount == 24.0 and not refunded.line_ids.restock,
      refunded.refund_amount)

# ---------------------------------------------------------------- workflow
technician = env['res.users'].create({
    'name': 'PH7 Technician', 'login': 'ph7_tech_check',
    'group_ids': [(6, 0, [env.ref('sahal_pharmacy.group_pharmacy_technician').id,
                          env.ref('base.group_user').id])]})
try:
    with env.cr.savepoint():
        sealed.with_user(technician).action_approve()
    check("a technician cannot accept returned medicine", False, "accepted")
except Exception as e:
    check("a technician cannot accept returned medicine",
          "pharmacist's decision" in str(e), str(e)[:70])

try:
    with env.cr.savepoint():
        sealed.action_process()
    check("stock cannot be moved before the return is accepted", False, "processed")
except Exception as e:
    check("stock cannot be moved before the return is accepted",
          'Approve the return' in str(e), str(e)[:70])

sealed.action_approve()
check("acceptance is attributed to the pharmacist",
      sealed.pharmacist_id == env.user and sealed.state == 'approved')
check("the disposition is counted",
      sealed.restock_count == 1 and sealed.destroy_count == 0)

sealed.action_process()
check("a restockable return creates an incoming transfer",
      bool(sealed.picking_id) and sealed.state == 'done', sealed.picking_id.name)
check("the batch is carried on the transfer",
      good_lot in sealed.picking_id.move_ids.move_line_ids.mapped('lot_id'),
      sealed.picking_id.move_ids.move_line_ids.mapped('lot_id.name'))

destroy = make_return('damaged', restock=False)
destroy.action_approve()
scrap_before = env['stock.scrap'].search_count([])
destroy.action_process()
check("a non-restockable return is scrapped, not quietly kept",
      env['stock.scrap'].search_count([]) == scrap_before + 1)
check("it creates no incoming transfer", not destroy.picking_id)

# ---------------------------------------------------------------- refund document
refunded.action_approve()
refunded.action_refund()
check("refunding raises a real customer credit note",
      refunded.credit_note_id.move_type == 'out_refund',
      refunded.credit_note_id.move_type)
check("for the amount of the refundable lines",
      abs(refunded.credit_note_id.amount_untaxed - 24.0) < 0.01,
      refunded.credit_note_id.amount_untaxed)
try:
    with env.cr.savepoint():
        refunded.action_refund()
    check("a return cannot be refunded twice", False, "second credit note")
except Exception as e:
    check("a return cannot be refunded twice", 'already exists' in str(e), str(e)[:70])

nothing_to_refund = make_return('opened', restock=False, refund=False)
nothing_to_refund.action_approve()
try:
    with env.cr.savepoint():
        nothing_to_refund.action_refund()
    check("refunding nothing is refused with an explanation", False, "created")
except Exception as e:
    check("refunding nothing is refused with an explanation",
          'marked for refund' in str(e), str(e)[:70])

print("\nPH7 CUSTOMER RETURNS: %d PASS / %d FAIL" % (PASS, FAIL))
env.cr.rollback()
print("rolled back")
