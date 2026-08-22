"""Service account, meter lifecycle and billing-cycle checks (SRS 5.2, 6, 7.2).


    odoo shell -d <database> --no-http --logfile=/dev/null \
      < addons/utility_management/tests/p1_service_account_check.py

Everything is rolled back at the end, so it is safe on production. Expect 30 PASS / 0 FAIL.
"""
from odoo import fields
PASS = FAIL = 0
def check(label, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1; print("  PASS  %s" % label)
    else:
        FAIL += 1; print("  FAIL  %s %s" % (label, detail))

partner = env['res.partner'].create({'name': 'P1 Smoke Customer'})
tariff = env['utility.tariff'].create({
    'name': 'P1 Smoke Flat', 'utility_type': 'electricity', 'structure': 'flat',
    'flat_rate': 0.5, 'base_charge': 10.0, 'unit_name': 'kWh'})
cycle = env.ref('utility_management.billing_cycle_monthly')

acct = env['utility.service.account'].create({
    'partner_id': partner.id, 'utility_type': 'electricity',
    'tariff_id': tariff.id, 'billing_cycle_id': cycle.id})
check("account number generated", acct.name.startswith('ELE/'), acct.name)
check("account starts pending", acct.state == 'pending')

m1 = env['utility.meter'].create({
    'name': 'P1-SMOKE-1', 'utility_type': 'electricity', 'digits': 6, 'state': 'active'})
inst = env['utility.meter.installation'].create({
    'meter_id': m1.id, 'account_id': acct.id, 'date_installed': '2026-01-01',
    'initial_reading': 0.0, 'reason': 'installed'})
check("installation sets meter.account_id", m1.account_id == acct)
check("meter partner flows from account", m1.partner_id == partner, m1.partner_id.name)
check("effective tariff from account", m1.effective_tariff_id == tariff)

# a second meter cannot be installed on a foreign utility type
# A savepoint, because a ValidationError leaves the offending row in the transaction:
# without it the rejected installation would still be counted in the history below.
try:
    with env.cr.savepoint():
        m_water = env['utility.meter'].create(
            {'name': 'P1-SMOKE-W', 'utility_type': 'water'})
        env['utility.meter.installation'].create({
            'meter_id': m_water.id, 'account_id': acct.id,
            'date_installed': '2026-01-01'})
    check("utility mismatch refused", False, "no error raised")
except Exception as e:
    check("utility mismatch refused", 'measures' in str(e), str(e)[:60])

acct.action_activate()
check("activate sets connection date", acct.state == 'active' and acct.connection_date)

r1 = env['utility.meter.reading'].create({
    'meter_id': m1.id, 'reading_date': '2026-02-01 08:00:00', 'present_reading': 100.0})
r2 = env['utility.meter.reading'].create({
    'meter_id': m1.id, 'reading_date': '2026-03-01 08:00:00', 'present_reading': 250.0})
check("reading inherits account", r2.account_id == acct)
check("consumption computed", r2.consumption == 150.0, r2.consumption)
(r1 | r2).action_validate()

invoices = (r1 | r2)._bill()
check("one invoice per service account", len(invoices) == 1, len(invoices))
inv = invoices[0]
check("invoice stamped with account", inv.utility_account_id == acct)
check("invoice dated at period end", str(inv.invoice_date) == '2026-03-01', inv.invoice_date)
check("due date from cycle (+14d)", str(inv.invoice_date_due) == '2026-03-15',
      inv.invoice_date_due)
check("period stamped", str(inv.utility_period_start) == '2026-02-01'
      and str(inv.utility_period_end) == '2026-03-01')
# 100 + 150 kWh at 0.5 plus two base charges of 10
check("invoice total = usage + base", abs(inv.amount_untaxed - (250 * 0.5 + 20)) < 0.01,
      inv.amount_untaxed)
check("account last_billed set", str(acct.last_billed_date) == '2026-03-01',
      acct.last_billed_date)

# --- meter replacement ---
m2 = env['utility.meter'].create({
    'name': 'P1-SMOKE-2', 'utility_type': 'electricity', 'digits': 6})
wiz = env['utility.meter.replace.wizard'].create({
    'old_meter_id': m1.id, 'final_reading': 300.0, 'new_meter_id': m2.id,
    'initial_reading': 0.0, 'date': '2026-03-15', 'reason': 'replaced'})
wiz.action_replace()
inst.invalidate_recordset()
check("old installation closed", str(inst.date_removed) == '2026-03-15', inst.date_removed)
check("final reading recorded", inst.final_reading == 300.0)
check("old meter decommissioned", m1.state == 'decommissioned', m1.state)
check("new meter active on account", m2.state == 'active' and m2.account_id == acct)
check("history has two rows", len(acct.installation_ids) == 2, len(acct.installation_ids))
closing = env['utility.meter.reading'].search(
    [('meter_id', '=', m1.id), ('reading_type', '=', 'replacement')])
opening = env['utility.meter.reading'].search(
    [('meter_id', '=', m2.id), ('reading_type', '=', 'opening')])
check("closing reading written", len(closing) == 1 and closing.present_reading == 300.0)
check("closing consumption is the last slice", closing.consumption == 50.0,
      closing.consumption)
check("opening reading starts new meter at zero", len(opening) == 1
      and opening.consumption == 0.0, opening.consumption)
check("both witnessed readings validated",
      closing.state == 'validated' and opening.state == 'validated')

# a replacement meter cannot be double-installed
try:
    with env.cr.savepoint():
        env['utility.meter.installation'].create({
            'meter_id': m2.id, 'account_id': acct.id, 'date_installed': '2026-04-01'})
    check("double installation refused", False, "no error raised")
except Exception as e:
    check("double installation refused", 'already installed' in str(e), str(e)[:60])

# --- balance + search ---
inv.action_post()
acct.invalidate_recordset()
check("balance reflects posted invoice", acct.balance > 0, acct.balance)
found = env['utility.service.account'].search([('balance', '>', 0)])
check("balance is searchable", acct in found)

# --- cron due-ness through the cycle ---
check("cycle not due right after billing", not cycle.is_due(fields.Date.to_date('2026-03-01'),
      fields.Date.to_date('2026-03-20')))
check("cycle due after a month", cycle.is_due(fields.Date.to_date('2026-03-01'),
      fields.Date.to_date('2026-04-02')))

print("\nP1 SMOKE: %d PASS / %d FAIL" % (PASS, FAIL))
env.cr.rollback()
print("rolled back")
