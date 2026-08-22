"""Billing run: reproducibility, controls, warnings and invoicing (SRS 12, 34, 35).

    odoo shell -d <database> --no-http --logfile=/dev/null \
      < addons/utility_management/tests/p4_billing_run_check.py

Rolls back at the end. Expect 26 PASS / 0 FAIL.
"""
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


env.user.group_ids = [(4, env.ref('utility_management.group_utility_manager').id)]
Run = env['utility.billing.run']
Reading = env['utility.meter.reading']

partner = env['res.partner'].create({'name': 'P4 Customer'})
tariff = env['utility.tariff'].create({
    'name': 'P4 Flat', 'utility_type': 'electricity', 'structure': 'flat',
    'flat_rate': 2.0, 'base_charge': 10.0, 'unit_name': 'kWh'})
cycle = env['utility.billing.cycle'].create({
    'name': 'P4 Monthly', 'frequency': 'monthly', 'anchor_day': 1, 'due_days': 10})


def make_account(name_suffix, estimation='avg3'):
    acct = env['utility.service.account'].create({
        'partner_id': partner.id, 'utility_type': 'electricity', 'tariff_id': tariff.id,
        'billing_cycle_id': cycle.id, 'estimation_method': estimation})
    acct.action_activate()
    meter = env['utility.meter'].create({
        'name': 'P4-%s' % name_suffix, 'utility_type': 'electricity', 'digits': 6,
        'state': 'active'})
    env['utility.meter.installation'].create({
        'meter_id': meter.id, 'account_id': acct.id, 'date_installed': '2026-01-01'})
    return acct, meter


def read(meter, date, value, validate=True, **kw):
    vals = {'meter_id': meter.id, 'reading_date': date, 'present_reading': value}
    vals.update(kw)
    r = Reading.create(vals)
    if validate:
        r.action_validate()
    return r


acct_a, meter_a = make_account('A')
acct_b, meter_b = make_account('B')
acct_c, meter_c = make_account('C')          # no reading at all this period
read(meter_a, '2026-01-31 08:00:00', 100.0)
read(meter_b, '2026-01-31 08:00:00', 100.0)

# ---------------------------------------------------------------- compute
run = Run.create({'period_start': '2026-02-01', 'period_end': '2026-02-28',
                  'billing_cycle_id': cycle.id, 'utility_type': 'electricity'})
check("run gets a reference", run.name.startswith('BRUN/'), run.name)
read(meter_a, '2026-02-28 08:00:00', 300.0)
read(meter_b, '2026-02-28 08:00:00', 250.0)
run.action_compute()
check("compute moves the run to review", run.state == 'review', run.state)
flagged = run.line_ids.filtered(lambda l: l.exception_state == 'review')
check("a high-usage reading is held out of billing until reviewed",
      flagged and not flagged[0].is_invoiceable(), flagged.mapped('exception_state'))
check("held lines are called out in the warnings",
      'unresolved exception' in (run.warning_text or ''), run.warning_text)
# Approve it the way a billing specialist would, then recompute so the run reflects it.
flagged.mapped('reading_id').action_approve_exception()
run.action_compute()
lines = run.line_ids
check("one line per active meter", len(lines) == 3, len(lines))
check("consumption picked up", run.total_consumption == 200.0 + 150.0,
      run.total_consumption)
check("amount = usage x rate + base charge",
      abs(run.total_amount - ((200 * 2 + 10) + (150 * 2 + 10))) < 0.01, run.total_amount)
gap = lines.filtered(lambda l: l.account_id == acct_c)
check("a meter with no reading still gets a line", len(gap) == 1)
check("the gap is explained on the line", 'No reading' in (gap.note or ''), gap.note)
check("the gap is not invoiceable", not gap.is_invoiceable())
check("missing readings raise a warning", 'no reading at all' in (run.warning_text or ''),
      run.warning_text)

# ---------------------------------------------------------------- reproducibility
first_amount = run.total_amount
first_signature = sorted((l.meter_id.id, l.consumption, l.amount) for l in run.line_ids)
run.action_compute()
second_signature = sorted((l.meter_id.id, l.consumption, l.amount) for l in run.line_ids)
check("recomputing a run reproduces it exactly", first_signature == second_signature)
check("recomputing does not double the totals", run.total_amount == first_amount,
      run.total_amount)

# ---------------------------------------------------------------- approval control
technician = env['res.users'].create({
    'name': 'P4 Billing Clerk', 'login': 'p4_clerk_check',
    'group_ids': [(6, 0, [env.ref('utility_management.group_utility_billing').id,
                          env.ref('base.group_user').id])]})
try:
    with env.cr.savepoint():
        run.with_user(technician).action_approve()
    check("a billing clerk cannot approve a run", False, "no error raised")
except Exception as e:
    check("a billing clerk cannot approve a run", 'Utility Manager' in str(e), str(e)[:60])

try:
    with env.cr.savepoint():
        run.action_generate_invoices()
    check("invoices cannot be generated before approval", False, "no error raised")
except Exception as e:
    check("invoices cannot be generated before approval", 'must be approved' in str(e),
          str(e)[:60])

run.action_approve()
check("approval is attributed", run.approved_by == env.user and run.approved_on)
check("approval locks the readings behind it",
      all(r.billing_run_id == run for r in run.line_ids.mapped('reading_id')))

# ---------------------------------------------------------------- period lock
locked = run.line_ids.mapped('reading_id')[0]
try:
    with env.cr.savepoint():
        locked.present_reading = 999.0
    check("a locked reading cannot be edited", False, "no error raised")
except Exception as e:
    check("a locked reading cannot be edited", 'has been approved' in str(e), str(e)[:70])

try:
    with env.cr.savepoint():
        run.action_compute()
    check("an approved run cannot be recomputed", False, "no error raised")
except Exception as e:
    check("an approved run cannot be recomputed", 'draft run' in str(e), str(e)[:60])

# ---------------------------------------------------------------- invoicing
run.action_generate_invoices()
check("run is now invoiced", run.state == 'invoiced', run.state)
check("one invoice per service account with a reading", len(run.invoice_ids) == 2,
      len(run.invoice_ids))
check("invoices carry the run", all(i.utility_billing_run_id == run
                                    for i in run.invoice_ids))
check("invoices carry the service account",
      all(i.utility_account_id for i in run.invoice_ids))
check("invoice total matches the run line",
      abs(sum(run.invoice_ids.mapped('amount_untaxed')) - first_amount) < 0.01,
      sum(run.invoice_ids.mapped('amount_untaxed')))
check("billed readings are marked",
      all(r.state == 'billed' for r in run.line_ids.mapped('reading_id')))

try:
    with env.cr.savepoint():
        run.action_cancel()
    check("an invoiced run cannot be cancelled", False, "no error raised")
except Exception as e:
    check("an invoiced run cannot be cancelled", 'Reverse those invoices' in str(e),
          str(e)[:60])

# ---------------------------------------------------------------- duplicate billing
run2 = Run.create({'period_start': '2026-02-01', 'period_end': '2026-02-28',
                   'billing_cycle_id': cycle.id, 'utility_type': 'electricity'})
run2.action_compute()
check("a second run over the same period finds nothing left to bill",
      all(not l.is_invoiceable() for l in run2.line_ids),
      [(l.meter_id.name, l.reading_id.name if l.reading_id else None)
       for l in run2.line_ids])

# ---------------------------------------------------------------- estimation in a run
run3 = Run.create({'period_start': '2026-03-01', 'period_end': '2026-03-31',
                   'billing_cycle_id': cycle.id, 'utility_type': 'electricity',
                   'create_estimates': True})
run3.action_compute()
estimated = run3.line_ids.filtered('is_estimated')
check("missing readings are estimated when the run asks", len(estimated) >= 2,
      len(estimated))
check("estimated share raises a warning", 'estimated readings' in (run3.warning_text or ''),
      run3.warning_text)

# ---------------------------------------------------------------- drop warning
acct_d, meter_d = make_account('D')
read(meter_d, '2026-04-29 08:00:00', 10.0)
read(meter_d, '2026-04-30 08:00:00', 12.0)    # a trickle next to February's 350 units
run4 = Run.create({'period_start': '2026-04-01', 'period_end': '2026-04-30',
                   'billing_cycle_id': cycle.id, 'utility_type': 'electricity',
                   'account_ids': [(6, 0, acct_d.ids)]})
run4.action_compute()
check("a collapse in billing is flagged against the previous run",
      'down' in (run4.warning_text or '').lower(), run4.warning_text)
check("the comparison names the run it compared with", run4.previous_run_id == run,
      run4.previous_run_id.name if run4.previous_run_id else None)

print("\nP4 BILLING RUN: %d PASS / %d FAIL" % (PASS, FAIL))
env.cr.rollback()
print("rolled back")
