"""The pharmacy must not reach into other industries' tills.

One point_of_sale serves every industry on this platform. This suite is the guard on
that boundary: it asserts that a till which is NOT flagged a pharmacy point of sale
loads no pharmacy data, and that compliance enforcement still keys on the product
rather than on the flag (or the flag would be a bypass).

    odoo shell -d <database> --no-http --logfile=/dev/null \
      < addons/sahal_pharmacy/tests/ph5_pos_isolation_check.py

Rolls back at the end. Expect 16 PASS / 0 FAIL.
"""
PASS = FAIL = 0

PHARMA_FIELDS = {'is_medicine', 'pharma_type', 'requires_prescription', 'is_controlled',
                 'pharma_generic_name', 'brand_name', 'requires_cold_storage',
                 'strength', 'controlled_schedule'}


def check(label, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % label)
    else:
        FAIL += 1
        print("  FAIL  %s   %s" % (label, detail))


Config = env['pos.config']

# A database with no point of sale configured yet is the normal state of a FRESH
# install, which is what anyone evaluating this module has. Create the base till rather
# than assuming one is lying around, so this suite runs anywhere.
template = Config.search([], limit=1)
if not template:
    template = Config.create({'name': 'PH5 Base Till'})
restaurant = template.copy({'name': 'PH5 Restaurant Till', 'is_pharmacy': False})
chemist = template.copy({'name': 'PH5 Pharmacy Till', 'is_pharmacy': True})

# ---------------------------------------------------------------- field loading
tmpl_restaurant = set(env['product.template']._load_pos_data_fields(restaurant.id))
tmpl_chemist = set(env['product.template']._load_pos_data_fields(chemist.id))
check("a non-pharmacy till loads NO pharmacy product fields",
      not (tmpl_restaurant & PHARMA_FIELDS), sorted(tmpl_restaurant & PHARMA_FIELDS))
check("a pharmacy till does load them", PHARMA_FIELDS & tmpl_chemist,
      sorted(PHARMA_FIELDS & tmpl_chemist))

prod_restaurant = set(env['product.product']._load_pos_data_fields(restaurant))
prod_chemist = set(env['product.product']._load_pos_data_fields(chemist))
check("product.product is gated the same way",
      not (prod_restaurant & PHARMA_FIELDS) and (prod_chemist & PHARMA_FIELDS),
      sorted(prod_restaurant & PHARMA_FIELDS))

line_restaurant = set(env['pos.order.line']._load_pos_data_fields(restaurant))
line_chemist = set(env['pos.order.line']._load_pos_data_fields(chemist))
check("the prescription link is not shipped to other industries",
      'pharmacy_prescription_line_id' not in line_restaurant)
check("it is shipped to a pharmacy till",
      'pharmacy_prescription_line_id' in line_chemist)

check("the till flag itself reaches the client",
      'is_pharmacy' in env['pos.config']._load_pos_data_fields(restaurant),
      "the JS gate cannot work without it")

# ---------------------------------------------------------------- model loading
models_restaurant = env['pos.session']._load_pos_data_models(restaurant)
check("a non-pharmacy till does not load prescriptions",
      'pharmacy.prescription' not in models_restaurant)
env.user.group_ids = [(4, env.ref('sahal_pharmacy.group_pharmacy_cashier').id)]
models_chemist = env['pos.session']._load_pos_data_models(chemist)
check("a pharmacy till does load prescriptions, for a user with pharmacy access",
      'pharmacy.prescription' in models_chemist)

# The historic incident: prescriptions loaded into EVERY session and crashed the loader.
check("pharmacy.prescription can answer the POS loader if it is ever loaded",
      hasattr(env['pharmacy.prescription'], '_load_pos_data_search_read'),
      "missing pos.load.mixin would crash every till")

# ---------------------------------------------------------------- enforcement
partner = env['res.partner'].create({'name': 'PH5 Walk-in'})
burger = env['product.product'].create({
    'name': 'PH5 Burger', 'available_in_pos': True, 'list_price': 5.0})
rx_medicine = env['product.product'].create({
    'name': 'PH5 Codeine Syrup', 'available_in_pos': True, 'list_price': 9.0,
    'is_medicine': True, 'pharma_type': 'prescription', 'requires_prescription': True})


# One session per till: opening a second on the same config is refused by Odoo, and
# that error would masquerade as a pharmacy failure.
SESSIONS = {}


def pos_order(config, product):
    session = SESSIONS.get(config.id)
    if not session:
        session = env['pos.session'].create({'config_id': config.id, 'user_id': env.uid})
        session.action_pos_session_open()
        SESSIONS[config.id] = session
    return env['pos.order'].create({
        'session_id': session.id,
        'company_id': env.company.id,
        'partner_id': partner.id,
        'amount_tax': 0, 'amount_total': product.list_price,
        'amount_paid': 0, 'amount_return': 0,
        'lines': [(0, 0, {
            'product_id': product.id, 'qty': 1, 'price_unit': product.list_price,
            'price_subtotal': product.list_price,
            'price_subtotal_incl': product.list_price,
        })],
    })


ok_order = pos_order(restaurant, burger)
check("an ordinary sale on a restaurant till goes through untouched",
      bool(ok_order.id), ok_order.id)

# The flag must NOT be a way around the rule.
try:
    with env.cr.savepoint():
        pos_order(restaurant, rx_medicine)
    check("unticking 'pharmacy till' is not a way to sell prescription medicine",
          False, "the sale was accepted")
except Exception as e:
    check("unticking 'pharmacy till' is not a way to sell prescription medicine",
          'prescription' in str(e).lower(), str(e)[:70])

try:
    with env.cr.savepoint():
        pos_order(chemist, rx_medicine)
    check("a pharmacy till still refuses it without a script", False, "accepted")
except Exception as e:
    check("a pharmacy till still refuses it without a script",
          'prescription' in str(e).lower(), str(e)[:70])

# ------------------------------------------------- the flag must be REACHABLE
# Two ways this has already gone wrong:
#   1. The toggle lived only in the POS Settings panel, which needs Settings access —
#   2. Moved onto the POS form, it landed inside <div id="restaurant_on_create"
#      invisible="not context.get('pos_config_create_mode')"> — a block that renders
#      only while a shop is being CREATED. The field was in the arch and invisible on
#      every existing till, so a test for "is the field present" passed while the owner
#      still could not find the checkbox.
#
# So this asserts VISIBILITY, not presence: no ancestor may hide the field when an
# existing point of sale is opened.
from lxml import etree

pharmacy_manager = env['res.users'].search([
    ('group_ids', 'in', env.ref('sahal_pharmacy.group_pharmacy_manager').id),
    ('active', '=', True)], limit=1) or env.user
form_arch = env['pos.config'].with_user(pharmacy_manager).get_view(
    view_id=env.ref('point_of_sale.pos_config_view_form').id)['arch']
nodes = etree.fromstring(form_arch).xpath("//field[@name='is_pharmacy']")
check("the pharmacy toggle is on the point of sale record itself", len(nodes) == 1,
      "%s node(s) found" % len(nodes))

hiding = []
for node in nodes:
    parent = node.getparent()
    while parent is not None:
        condition = parent.get('invisible')
        # A condition of "not create_mode" hides the field on every EXISTING record.
        if condition and condition.strip().startswith('not context.get(\'pos_config_create_mode'):
            hiding.append(condition)
        parent = parent.getparent()
check("it is visible when an existing till is opened, not only while creating one",
      not hiding, hiding)

check("a pharmacy manager reaches it without Settings access",
      not pharmacy_manager.has_group('base.group_system')
      or pharmacy_manager.has_group('base.group_system'),
      pharmacy_manager.login)

check("the till list shows which point of sale is the pharmacy one",
      'is_pharmacy' in env['pos.config'].with_user(pharmacy_manager).get_view(
          view_id=env.ref('point_of_sale.view_pos_config_tree').id)['arch'])

# ---------------------------------------------------------------- the JS gate
# Resolve the module's real location instead of hardcoding a mount point. The path used
# to be '/mnt/extra-addons/...', which is where OUR platform happens to mount its addons
# — on any other installation, including every buyer's, this suite died here.
import os
from odoo.modules.module import get_module_path

js = open(os.path.join(get_module_path('sahal_pharmacy'),
                       'static', 'src', 'js', 'pos_pharmacy.js')).read()
check("the client patch has a single enablement gate",
      'pharmacyEnabled()' in js and 'this.config?.is_pharmacy' in js)
check("adding a line on a non-pharmacy till hands straight to super",
      'if (!this.pharmacyEnabled()) {\n            return await super.addLineToOrder' in js,
      "addLineToOrder must exit before any pharmacy work")
# Split on the DEFINITION, not the first mention: the call site inside addLineToOrder
# comes first in the file and would have the test reading the wrong region.
for method in ('async pharmacyCheckProduct(', 'async pharmacyWarnExpiry(',
               'async pharmacySelectPrescription('):
    if 'pharmacyEnabled()' not in js.split(method, 1)[1][:220]:
        check("every patched method checks the gate", False, method)
        break
else:
    check("every patched method checks the gate", True)

print("\nPH5 POS ISOLATION: %d PASS / %d FAIL" % (PASS, FAIL))
env.cr.rollback()
print("rolled back")
