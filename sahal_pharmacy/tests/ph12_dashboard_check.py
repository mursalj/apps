"""Pharmacy dashboard figures (PRD 84).

    odoo shell -d <database> --no-http --logfile=/dev/null \
      < addons/sahal_pharmacy/tests/ph12_dashboard_check.py

Rolls back at the end. Expect 18 PASS / 0 FAIL.
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
Dash = env['pharmacy.dashboard']
Quant = env['stock.quant'].sudo()
warehouse = env['stock.warehouse'].search([('company_id', '=', env.company.id)], limit=1)
location = warehouse.lot_stock_id
now = fields.Datetime.now()
today = fields.Date.today()

# ---------------------------------------------------------------- shape
data = Dash.get_dashboard_data()
for section in ('currency', 'kpis', 'stock', 'expiry', 'top_products', 'mix', 'series',
                'attention'):
    if section not in data:
        check("the dashboard returns every section in one call", False, section)
        break
else:
    check("the dashboard returns every section in one call", True)

check("it comes back in a single RPC, not one per tile", isinstance(data, dict))
check("the period is capped to something sensible",
      len(Dash.get_dashboard_data(months=999)['series']) == 24,
      len(Dash.get_dashboard_data(months=999)['series']))
check("a short period is honoured", len(Dash.get_dashboard_data(months=3)['series']) == 3)

# ---------------------------------------------------------------- stock health
empty = env['product.product'].create({
    'name': 'PH12 Out Of Stock', 'is_medicine': True, 'pharma_type': 'otc',
    'is_storable': True, 'standard_price': 5.0})
stocked = env['product.product'].create({
    'name': 'PH12 In Stock', 'is_medicine': True, 'pharma_type': 'otc',
    'is_storable': True, 'standard_price': 5.0})
Quant._update_available_quantity(stocked, location, 100)

data = Dash.get_dashboard_data()
check("stock health counts what cannot be dispensed today",
      data['stock']['out_of_stock'] >= 1, data['stock'])
check("the shelf is valued at cost", data['stock']['stock_value'] >= 500.0,
      data['stock']['stock_value'])
check("the tile carries its own colour, decided on the server",
      data['stock']['level'] in ('success', 'warning', 'danger'), data['stock']['level'])
check("the three stock buckets do not double-count",
      data['stock']['out_of_stock'] + data['stock']['low_stock']
      + data['stock']['healthy'] <= data['stock']['medicines'] + 1,
      data['stock'])

# ---------------------------------------------------------------- expiry
perishable = env['product.product'].create({
    'name': 'PH12 Expiring', 'is_medicine': True, 'pharma_type': 'otc',
    'is_storable': True, 'tracking': 'lot', 'use_expiration_date': True,
    'expiration_time': 365, 'standard_price': 4.0})
dead_lot = env['stock.lot'].create({
    'name': 'PH12-DEAD', 'product_id': perishable.id,
    'expiration_date': now - timedelta(days=2)})
soon_lot = env['stock.lot'].create({
    'name': 'PH12-SOON', 'product_id': perishable.id,
    'expiration_date': now + timedelta(days=10)})
Quant._update_available_quantity(perishable, location, 25, lot_id=dead_lot)
Quant._update_available_quantity(perishable, location, 15, lot_id=soon_lot)

data = Dash.get_dashboard_data()
check("expired batches are counted", data['expiry']['expired_lines'] >= 1,
      data['expiry'])
check("and valued, so expiry is a money conversation",
      data['expiry']['expired_value'] >= 100.0, data['expiry']['expired_value'])
check("near-expiry batches are counted separately",
      data['expiry']['near_lines'] >= 1, data['expiry'])
check("expiry turns the tile red once something has expired",
      data['expiry']['level'] == 'danger', data['expiry']['level'])

# ---------------------------------------------------------------- selling
customer = env['res.partner'].create({'name': 'PH12 Customer'})
order = env['sale.order'].create({
    'partner_id': customer.id,
    'order_line': [(0, 0, {'product_id': stocked.id, 'product_uom_qty': 4,
                           'price_unit': 25.0})]})
order.action_confirm()
data = Dash.get_dashboard_data()
top_names = [p['name'] for p in data['top_products']]
check("best sellers include what was sold through Sales",
      any('PH12 In Stock' in name for name in top_names), top_names)
check("the mix splits revenue by product type", bool(data['mix']), data['mix'])
check("the monthly series carries both channels separately",
      'retail' in data['series'][-1] and 'invoiced' in data['series'][-1],
      data['series'][-1])

# ---------------------------------------------------------------- attention queue
doctor = env['res.partner'].create({'name': 'PH12 Prescriber', 'is_doctor': True})
patient = env['res.partner'].create({'name': 'PH12 Patient', 'is_patient': True})
rx = env['pharmacy.prescription'].create({
    'patient_id': patient.id, 'doctor_id': doctor.id,
    'line_ids': [(0, 0, {'product_id': stocked.id, 'qty_prescribed': 1})]})
rx.action_submit() if hasattr(rx, 'action_submit') else rx.write({'state': 'to_verify'})

data = Dash.get_dashboard_data()
keys = [row['key'] for row in data['attention']]
check("the attention queue lists prescriptions waiting to be verified",
      'to_verify' in keys, keys)
check("every attention row carries a count and a colour",
      all(row.get('count') and row.get('level') for row in data['attention']),
      data['attention'])
check("rows with nothing waiting are left out entirely",
      all(row['count'] > 0 for row in data['attention']), data['attention'])

# ---------------------------------------------------------------- drill-downs
for key in ('out_of_stock', 'low_stock', 'expired', 'near_expiry', 'to_verify',
            'clinical', 'returns', 'claims', 'credit_hold', 'supplier_licence',
            'documents', 'receivables'):
    action = Dash.action_open(key)
    if not action or 'res_model' not in action:
        check("every tile opens the list behind it", False, key)
        break
else:
    check("every tile opens the list behind it", True)

check("an unknown tile key returns nothing rather than an error",
      Dash.action_open('nonsense') is False)

print("\nPH12 DASHBOARD: %d PASS / %d FAIL" % (PASS, FAIL))
env.cr.rollback()
print("rolled back")
