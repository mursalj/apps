"""Reporting dimensions for product type, wholesale and suppliers (PRD 78-84).

    odoo shell -d <database> --no-http --logfile=/dev/null \
      < addons/sahal_pharmacy/tests/ph4_reporting_check.py

Rolls back at the end. Expect 12 PASS / 0 FAIL.
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



# ---------------------------------------------------------------- dimensions exist
Report = env['sale.report']
for field in ('pharma_type', 'generic_id', 'dosage_form_id', 'is_controlled',
              'is_wholesale_customer', 'wholesale_category'):
    if field not in Report._fields:
        check("sales analysis carries the pharmaceutical dimensions", False, field)
        break
else:
    check("sales analysis carries the pharmaceutical dimensions", True)

Quant = env['stock.quant']
for field in ('expiry_status', 'days_to_expiry', 'stock_value', 'pharma_type'):
    if field not in Quant._fields:
        check("stock analysis carries the expiry dimensions", False, field)
        break
else:
    check("stock analysis carries the expiry dimensions", True)

# ---------------------------------------------------------------- views exist
for xmlid, model in (
        ('view_sale_report_pharmacy_pivot', 'sale.report'),
        ('view_sale_report_pharmacy_graph', 'sale.report'),
        ('view_quant_pharmacy_pivot', 'stock.quant'),
        ('view_wholesale_credit_list', 'res.partner'),
        ('view_supplier_return_pivot', 'pharmacy.supplier.return')):
    view = env.ref('sahal_pharmacy.%s' % xmlid, raise_if_not_found=False)
    if not view or view.model != model:
        check("the module now has graph and pivot views", False, xmlid)
        break
else:
    check("the module now has graph and pivot views", True)

# ---------------------------------------------------------------- expiry maths
from datetime import timedelta
from odoo import fields

product = env['product.product'].create({
    'name': 'PH4 Expiry Probe', 'is_medicine': True, 'pharma_type': 'otc',
    'tracking': 'lot', 'use_expiration_date': True, 'standard_price': 3.0,
    'is_storable': True})
now = fields.Datetime.now()
lots = {}
for key, days in (('expired', -10), ('near', 5), ('valid', 400)):
    lots[key] = env['stock.lot'].create({
        'name': 'PH4-%s' % key, 'product_id': product.id,
        'expiration_date': now + timedelta(days=days)})

location = env['stock.location'].search([('usage', '=', 'internal')], limit=1)
quants = {}
for key, lot in lots.items():
    quants[key] = env['stock.quant'].create({
        'product_id': product.id, 'lot_id': lot.id, 'location_id': location.id,
        'quantity': 10.0, 'inventory_quantity_auto_apply': 10.0})

check("an out-of-date batch reads expired",
      quants['expired'].expiry_status == 'expired', quants['expired'].expiry_status)
check("a batch inside the alert window reads near expiry",
      quants['near'].expiry_status == 'near', quants['near'].expiry_status)
check("a long-dated batch reads valid",
      quants['valid'].expiry_status == 'valid', quants['valid'].expiry_status)
check("days remaining is reported",
      quants['near'].days_to_expiry in (4, 5), quants['near'].days_to_expiry)
check("stock is valued so expiry is a financial number",
      quants['expired'].stock_value == 30.0, quants['expired'].stock_value)

# the status must be searchable, or it cannot drive a filter or a dashboard
expired_found = env['stock.quant'].search([
    ('expiry_status', '=', 'expired'), ('product_id', '=', product.id)])
near_found = env['stock.quant'].search([
    ('expiry_status', '=', 'near'), ('product_id', '=', product.id)])
check("expired stock can be searched", quants['expired'] in expired_found,
      expired_found.ids)
check("near-expiry stock can be searched", quants['near'] in near_found,
      near_found.ids)
check("searching does not mix the two", quants['valid'] not in (expired_found | near_found))

# ---------------------------------------------------------------- grouping works
groups = env['stock.quant']._read_group(
    [('product_id', '=', product.id)], ['product_id'], ['quantity:sum'])
check("stock groups by product for a pivot", bool(groups), groups)

no_expiry = env['product.product'].create({
    'name': 'PH4 No Expiry', 'is_medicine': True, 'pharma_type': 'device',
    'is_storable': True})
plain_quant = env['stock.quant'].create({
    'product_id': no_expiry.id, 'location_id': location.id, 'quantity': 4.0,
    'inventory_quantity_auto_apply': 4.0})
check("a product without expiry is not reported as expiring",
      plain_quant.expiry_status == 'none', plain_quant.expiry_status)

print("\nPH4 REPORTING: %d PASS / %d FAIL" % (PASS, FAIL))
env.cr.rollback()
print("rolled back")
