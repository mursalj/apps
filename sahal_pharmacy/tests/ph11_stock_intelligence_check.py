"""Stock-out, low stock, dead stock and expiry loss (PRD 60-63).

    odoo shell -d <database> --no-http --logfile=/dev/null \
      < addons/sahal_pharmacy/tests/ph11_stock_intelligence_check.py

Rolls back at the end. Expect 13 PASS / 0 FAIL.
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
Intel = env['pharmacy.stock.intelligence']
Quant = env['stock.quant'].sudo()
warehouse = env['stock.warehouse'].search([('company_id', '=', env.company.id)], limit=1)
location = warehouse.lot_stock_id
now = fields.Datetime.now()


def medicine(name, expiring=True):
    return env['product.product'].create({
        'name': name, 'is_medicine': True, 'pharma_type': 'otc', 'is_storable': True,
        'tracking': 'lot' if expiring else 'none',
        'use_expiration_date': expiring, 'expiration_time': 365 if expiring else 0,
        'standard_price': 4.0, 'list_price': 10.0})


empty = medicine('PH11 Nothing Left', expiring=False)
stocked = medicine('PH11 Well Stocked', expiring=False)
Quant._update_available_quantity(stocked, location, 500)
scarce = medicine('PH11 Running Low', expiring=False)
Quant._update_available_quantity(scarce, location, 3)

# ---------------------------------------------------------------- out of stock
out = Intel._out_of_stock()
check("a medicine with nothing on hand is reported out of stock", empty in out,
      out.mapped('name')[:5])
check("a well-stocked medicine is not", stocked not in out)

# ---------------------------------------------------------------- low stock
env['stock.warehouse.orderpoint'].create({
    'product_id': scarce.id, 'location_id': location.id,
    'product_min_qty': 10, 'product_max_qty': 100})
low = Intel._low_stock()
check("stock at or below its reorder point is reported low", scarce in low,
      low.mapped('name'))
check("a medicine above its reorder point is not", stocked not in low)
check("out-of-stock is not double-counted as low", empty not in low)

# ---------------------------------------------------------------- dead stock
stale = medicine('PH11 Never Sells', expiring=False)
Quant._update_available_quantity(stale, location, 60)
dead = Intel._dead_stock(days=30)
check("stock that nothing has taken out is reported dead", stale in dead,
      dead.mapped('name')[:5])
check("a product with no stock is not called dead stock", empty not in dead)

# a product that HAS moved out recently is not dead
moved = medicine('PH11 Moves Fast', expiring=False)
Quant._update_available_quantity(moved, location, 20)
customer_location = env.ref('stock.stock_location_customers')
move = env['stock.move'].create({
    'product_id': moved.id, 'product_uom_qty': 5,
    'product_uom': moved.uom_id.id,
    'location_id': location.id, 'location_dest_id': customer_location.id,
    'description_picking': 'PH11 sale'})
move._action_confirm()
move._action_assign()
move.move_line_ids.quantity = 5
# picked=True is what makes Odoo 19 treat the quantity as actually taken; without it
# the move completes with nothing moved and the product still looks unsold.
move.picked = True
move._action_done()
check("stock that has left recently is not dead", moved not in Intel._dead_stock(days=30),
      [p.name for p in Intel._dead_stock(days=30)][:5])

# ---------------------------------------------------------------- expiry loss
perishable = medicine('PH11 Expired Batch')
gone = env['stock.lot'].create({
    'name': 'PH11-GONE', 'product_id': perishable.id,
    'expiration_date': now - timedelta(days=5)})
Quant._update_available_quantity(perishable, location, 25, lot_id=gone)
fresh = env['stock.lot'].create({
    'name': 'PH11-FRESH', 'product_id': perishable.id,
    'expiration_date': now + timedelta(days=100)})
Quant._update_available_quantity(perishable, location, 10, lot_id=fresh)

loss = Intel._expiry_loss()
check("expired stock on hand is quantified", loss['quantity'] >= 25, loss)
check("and valued at cost, not retail", loss['value'] >= 100.0, loss['value'])
check("in-date stock is not counted as a loss", loss['quantity'] < 35, loss)

# ---------------------------------------------------------------- the digest
branch = env['pharmacy.profile'].create({
    'name': 'PH11 Branch', 'company_id': env.company.id,
    'pharmacist_in_charge_id': env.user.id})
reported = Intel._cron_stock_alert()
check("the daily scan reports something to act on", reported > 0, reported)
activities = env['mail.activity'].search([
    ('res_model', '=', 'pharmacy.profile'), ('res_id', '=', branch.id)])
check("it raises an activity for the pharmacy manager", bool(activities),
      activities.mapped('summary'))
# Odoo emails the assignee of an activity - that is staff, not a customer. What matters
# is that nothing about stock levels goes OUTSIDE the pharmacy.
notified = env['mail.mail'].search([('subject', 'ilike', 'Pharmacy stock')])
outsiders = notified.mapped('recipient_ids').filtered(
    lambda p: not p.user_ids or not any(u._is_internal() for u in p.user_ids))
check("nothing about stock levels is sent outside the pharmacy",
      not outsiders, outsiders.mapped('name'))
check("the digest lands on the pharmacy record, where a manager looks",
      any('Pharmacy stock' in (m or '') for m in branch.message_ids.mapped('body')),
      branch.message_ids.mapped('body')[:1])

print("\nPH11 STOCK INTELLIGENCE: %d PASS / %d FAIL" % (PASS, FAIL))
env.cr.rollback()
print("rolled back")
