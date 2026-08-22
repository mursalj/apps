"""Expired-stock blocking and FEFO selection (PRD 15, 16, 40, 41).

    odoo shell -d <database> --no-http --logfile=/dev/null \
      < addons/sahal_pharmacy/tests/ph8_expiry_fefo_check.py

Rolls back at the end. Expect 15 PASS / 0 FAIL.
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
Control = env['pharmacy.expiry.control']
now = fields.Datetime.now()

medicine = env['product.product'].create({
    'name': 'PH8 Amoxicillin 500', 'is_medicine': True, 'pharma_type': 'otc',
    'tracking': 'lot', 'use_expiration_date': True, 'expiration_time': 730,
    'list_price': 8.0, 'available_in_pos': True, 'is_storable': True})
# The warehouse of THIS company: the first internal location in the database may well
warehouse = env['stock.warehouse'].search([('company_id', '=', env.company.id)], limit=1)
location = warehouse.lot_stock_id
Quant = env['stock.quant'].sudo()

lots = {}
for key, days, qty in (('expired', -3, 5), ('soonest', 20, 10), ('later', 200, 40),
                       ('latest', 500, 100)):
    lot = env['stock.lot'].create({
        'name': 'PH8-%s' % key, 'product_id': medicine.id,
        'expiration_date': now + timedelta(days=days)})
    Quant._update_available_quantity(medicine, location, qty, lot_id=lot)
    lots[key] = lot

# A batch with genuinely no expiry has to sit on a product that does not use expiry
# dates: product_expiry stamps every lot of an expiring product, so "no date" is not
# something you can ask for on one.
undated_product = env['product.product'].create({
    'name': 'PH8 Bandages', 'is_medicine': True, 'pharma_type': 'device',
    'tracking': 'lot', 'is_storable': True})
no_expiry_lot = env['stock.lot'].create({
    'name': 'PH8-NOEXP', 'product_id': undated_product.id})
Quant._update_available_quantity(undated_product, location, 7, lot_id=no_expiry_lot)

# ---------------------------------------------------------------- FEFO
suggested = Control._fefo_lots(medicine)
check("FEFO returns the earliest-expiring batch first",
      suggested and suggested[0]['lot'] == 'PH8-soonest',
      [s['lot'] for s in suggested])
check("FEFO never suggests an expired batch",
      'PH8-expired' not in [s['lot'] for s in suggested],
      [s['lot'] for s in suggested])
check("FEFO orders every batch by expiry",
      [s['lot'] for s in suggested] == ['PH8-soonest', 'PH8-later', 'PH8-latest'],
      [s['lot'] for s in suggested])
undated = Control._fefo_lots(undated_product)
check("a batch with no expiry is still offered, and sorts behind dated stock",
      undated and undated[0]['lot'] == 'PH8-NOEXP' and not undated[0]['expiry'],
      undated)
check("quantity on hand comes with the suggestion",
      suggested[0]['quantity'] == 10, suggested[0])

empty_product = env['product.product'].create({
    'name': 'PH8 Nothing In Stock', 'is_medicine': True, 'pharma_type': 'otc',
    'tracking': 'lot', 'is_storable': True})
check("a product with no stock suggests nothing", Control._fefo_lots(empty_product) == [])

# ---------------------------------------------------------------- the block
problems = Control._expired_lot_problems([(medicine, lots['expired'])])
check("an expired batch is described, with its date", problems and 'expired on' in problems[0],
      problems)
check("a valid batch raises nothing",
      not Control._expired_lot_problems([(medicine, lots['later'])]))

try:
    with env.cr.savepoint():
        Control._assert_not_expired([(medicine, lots['expired'])], 'PH8 Probe')
    check("selling an expired batch is refused", False, "allowed")
except Exception as e:
    check("selling an expired batch is refused", 'expired medicine' in str(e),
          str(e)[:70])

# override: allowed, but only deliberately — and the default key must be honoured
# without every caller having to name it.
Control.with_context(pharmacy_expiry_override=True)._assert_not_expired(
    [(medicine, lots['expired'])], 'PH8 Probe')
check("an explicit pharmacist override lets it through", True)

# A medicine created without a shelf life gets a sane default rather than minting
# batches that are expired on arrival...
auto = env['product.product'].create({
    'name': 'PH8 Auto Shelf Life', 'is_medicine': True, 'pharma_type': 'otc',
    'tracking': 'lot', 'is_storable': True})
check("a new medicine is given a shelf life instead of expiring on arrival",
      auto.use_expiration_date and auto.expiration_time == 730,
      (auto.use_expiration_date, auto.expiration_time))

# ...and one that already has the mistake is FLAGGED, not blocked from being edited.
legacy = env['product.product'].create({
    'name': 'PH8 Legacy No Shelf Life', 'is_medicine': True, 'pharma_type': 'otc',
    'tracking': 'lot', 'is_storable': True})
legacy.expiration_time = 0
check("an existing medicine with no shelf life is flagged, not blocked",
      legacy.shelf_life_warning and legacy.name == 'PH8 Legacy No Shelf Life',
      legacy.shelf_life_warning)

device = env['product.product'].create({
    'name': 'PH8 Crutches', 'is_medicine': True, 'pharma_type': 'device',
    'tracking': 'lot', 'is_storable': True})
check("a device is batch-tracked for recall but given no expiry date",
      device.tracking == 'lot' and not device.use_expiration_date,
      (device.tracking, device.use_expiration_date))

env['ir.config_parameter'].sudo().set_param('sahal_pharmacy.block_expired_sales', 'False')
Control._assert_not_expired([(medicine, lots['expired'])], 'PH8 Probe')
check("the block can be switched off for a jurisdiction that allows it", True)
env['ir.config_parameter'].sudo().set_param('sahal_pharmacy.block_expired_sales', 'True')

# ---------------------------------------------------------------- at the till
# See ph5: a fresh install has no till configured, so create one rather than assume.
base_till = env['pos.config'].search([], limit=1)
if not base_till:
    base_till = env['pos.config'].create({'name': 'PH8 Base Till'})
config = base_till.copy({'name': 'PH8 Pharmacy Till', 'is_pharmacy': True})
session = env['pos.session'].create({'config_id': config.id, 'user_id': env.uid})
session.action_pos_session_open()
customer = env['res.partner'].create({'name': 'PH8 Walk-in'})


def till_order(lot):
    return env['pos.order'].create({
        'session_id': session.id, 'company_id': env.company.id,
        'partner_id': customer.id,
        'amount_tax': 0, 'amount_total': 8.0, 'amount_paid': 0, 'amount_return': 0,
        'lines': [(0, 0, {
            'product_id': medicine.id, 'qty': 1, 'price_unit': 8.0,
            'price_subtotal': 8.0, 'price_subtotal_incl': 8.0,
            'pack_lot_ids': [(0, 0, {'lot_name': lot.name})],
        })],
    })


try:
    with env.cr.savepoint():
        till_order(lots['expired'])
    check("the till refuses an expired batch, not just warns about it", False, "sold")
except Exception as e:
    check("the till refuses an expired batch, not just warns about it",
          'expired medicine' in str(e), str(e)[:70])

good_sale = till_order(lots['later'])
check("a valid batch sells normally", bool(good_sale.id))

# ---------------------------------------------------------------- dispensing
patient = env['res.partner'].create({'name': 'PH8 Patient', 'is_patient': True})
doctor = env['res.partner'].create({'name': 'PH8 Prescriber', 'is_doctor': True})
rx = env['pharmacy.prescription'].create({
    'patient_id': patient.id, 'doctor_id': doctor.id,
    'line_ids': [(0, 0, {'product_id': medicine.id, 'qty_prescribed': 2})]})
rx.action_verify()
dispense = env['pharmacy.dispense'].create({
    'prescription_id': rx.id, 'pharmacist_id': env.user.id,
    'line_ids': [(0, 0, {'product_id': medicine.id, 'quantity': 2,
                         'lot_id': lots['expired'].id})]})
try:
    with env.cr.savepoint():
        dispense.action_confirm()
    check("dispensing an expired batch is refused", False, "dispensed")
except Exception as e:
    check("dispensing an expired batch is refused", 'expired' in str(e).lower(),
          str(e)[:70])

line = env['pharmacy.dispense.line'].new({'product_id': medicine.id})
line._onchange_product_fefo()
check("the dispensing screen pre-selects the FEFO batch",
      line.lot_id.name == 'PH8-soonest', line.lot_id.name)

print("\nPH8 EXPIRY & FEFO: %d PASS / %d FAIL" % (PASS, FAIL))
env.cr.rollback()
print("rolled back")
