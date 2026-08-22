"""Reading validation, rollover, estimation and true-up checks (SRS 8, 9, 10, 14).

    odoo shell -d <database> --no-http --logfile=/dev/null \
      < addons/utility_management/tests/p2_reading_validation_check.py

Rolls back at the end; safe to run anywhere. Expect 30 PASS / 0 FAIL.
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


Reading = env['utility.meter.reading']

# Ruling on an exception is gated on the Billing Specialist group. The shell user does not
# hold it, so grant it here (rolled back with everything else) and prove the gate first.
billing_group = env.ref('utility_management.group_utility_billing')
technician = env['res.users'].create({
    'name': 'P2 Field Tech', 'login': 'p2_field_tech_check',
    'group_ids': [(6, 0, [env.ref('utility_management.group_utility_technician').id,
                          env.ref('base.group_user').id])]})
partner = env['res.partner'].create({'name': 'P2 Check Customer'})
tariff = env['utility.tariff'].create({
    'name': 'P2 Flat', 'utility_type': 'electricity', 'structure': 'flat',
    'flat_rate': 1.0, 'unit_name': 'kWh'})
cycle = env.ref('utility_management.billing_cycle_monthly')


def new_account(method='avg3', minimum=0.0):
    acct = env['utility.service.account'].create({
        'partner_id': partner.id, 'utility_type': 'electricity', 'tariff_id': tariff.id,
        'billing_cycle_id': cycle.id, 'estimation_method': method,
        'minimum_consumption': minimum})
    acct.action_activate()
    return acct


def new_meter(acct, name, digits=6):
    meter = env['utility.meter'].create({
        'name': name, 'utility_type': 'electricity', 'digits': digits, 'state': 'active'})
    env['utility.meter.installation'].create({
        'meter_id': meter.id, 'account_id': acct.id, 'date_installed': '2026-01-01'})
    return meter


def read(meter, day, value, **kw):
    vals = {'meter_id': meter.id, 'reading_date': '2026-%s 08:00:00' % day,
            'present_reading': value}
    vals.update(kw)
    return Reading.create(vals)


# ---------------------------------------------------------------- rollover
acct = new_account()
m = new_meter(acct, 'P2-ROLL')
base = [read(m, '01-31', 100.0), read(m, '02-28', 200.0), read(m, '03-31', 300.0)]
for r in base:
    r.action_validate()
check("normal consumption", base[2].consumption == 100.0, base[2].consumption)
check("normal reading is clean", base[2].exception_state == 'normal',
      base[2].exception_codes)

near_max = read(m, '04-30', 999900.0)
near_max.action_validate()
wrapped = read(m, '05-31', 150.0)
check("rollover detected", wrapped.is_rollover, wrapped.exception_codes)
check("rollover consumption crosses the wrap", wrapped.consumption == 250.0,
      wrapped.consumption)
check("rollover is queued for review", wrapped.exception_state == 'review',
      wrapped.exception_state)

# A mis-key looks like a rollover but is far too big to be one.
acct2 = new_account()
m2 = new_meter(acct2, 'P2-MISKEY')
for day, val in (('01-31', 1000.0), ('02-28', 1100.0), ('03-31', 1200.0)):
    read(m2, day, val).action_validate()
miskey = read(m2, '04-30', 120.0)   # meant 1300, typed 120
check("implausible wrap is NOT called a rollover", not miskey.is_rollover)
check("mis-key is invalid, not billable", miskey.exception_state == 'invalid',
      miskey.exception_state)
check("mis-key was still recorded, not refused", bool(miskey.id))

# ---------------------------------------------------------------- anomalies
acct3 = new_account()
m3 = new_meter(acct3, 'P2-ANOM')
for day, val in (('01-31', 100.0), ('02-28', 200.0), ('03-31', 300.0)):
    read(m3, day, val).action_validate()
high = read(m3, '04-30', 700.0)
check("high usage flagged", 'high' in (high.exception_codes or ''), high.exception_codes)
low = read(m3, '05-31', 710.0)
check("low usage flagged", 'low' in (low.exception_codes or ''), low.exception_codes)
zero = read(m3, '06-30', 710.0)
check("zero consumption flagged", 'zero' in (zero.exception_codes or ''),
      zero.exception_codes)
dup = read(m3, '06-30', 715.0)
check("duplicate flagged", 'duplicate' in (dup.exception_codes or ''), dup.exception_codes)
future = Reading.create({
    'meter_id': m3.id, 'present_reading': 800.0,
    'reading_date': fields.Datetime.add(fields.Datetime.now(), days=3)})
check("future date flagged", 'future' in (future.exception_codes or ''),
      future.exception_codes)
tamper = read(m3, '07-31', 720.0, condition='tampered')
check("tampering flagged", tamper.exception_state == 'tampering', tamper.exception_state)

# ---------------------------------------------------------------- review workflow
try:
    with env.cr.savepoint():
        high.with_user(technician).action_approve_exception()
    check("a field technician may not clear an exception", False, "no error raised")
except Exception as e:
    check("a field technician may not clear an exception",
          'Billing Specialist' in str(e), str(e)[:70])

env.user.group_ids = [(4, billing_group.id)]
try:
    with env.cr.savepoint():
        miskey.action_validate()
    check("invalid reading cannot be validated", False, "no error raised")
except Exception as e:
    check("invalid reading cannot be validated", 'ruled on' in str(e), str(e)[:70])

high.action_approve_exception()
check("approval clears the exception", high.exception_state == 'normal')
check("approval is attributed", high.reviewed_by == env.user and high.reviewed_on)
high.action_validate()
check("approved reading validates", high.state == 'validated')
check("approved reading is billable", bool(high._billable()))

review_only = read(m3, '08-31', 5000.0)
review_only.action_validate()
check("unapproved exception validates but is NOT billable",
      review_only.state == 'validated' and not review_only._billable(),
      review_only.exception_state)

# ---------------------------------------------------------------- back-dated insert
acct4 = new_account()
m4 = new_meter(acct4, 'P2-BACKDATE')
jan = read(m4, '01-31', 100.0)
mar = read(m4, '03-31', 300.0)
check("march opens from january", mar.previous_reading == 100.0, mar.previous_reading)
feb = read(m4, '02-28', 200.0)
mar.invalidate_recordset()
check("back-dated insert re-opens march from february", mar.previous_reading == 200.0,
      mar.previous_reading)
check("back-dated insert corrects march consumption", mar.consumption == 100.0,
      mar.consumption)

# ---------------------------------------------------------------- query cost
acct5 = new_account()
m5 = new_meter(acct5, 'P2-BULK')
for i in range(1, 13):
    read(m5, '%02d-15' % i, i * 100.0)
env.flush_all()
env.invalidate_all()
batch = Reading.search([('meter_id', '=', m5.id)])

# Count the SELECTs the compute actually issues. The old implementation ran one search
# per record; at 100k meters that is the difference between a billing run and an outage.
queries = []
original_execute = type(env.cr).execute


def counting_execute(self, query, params=None, log_exceptions=None):
    queries.append(str(query))
    return original_execute(self, query, params)


type(env.cr).execute = counting_execute
try:
    batch.mapped('previous_reading')
finally:
    type(env.cr).execute = original_execute
reading_selects = [q for q in queries if 'utility_meter_reading' in q and 'SELECT' in q]
check("previous_reading for 12 readings costs a constant number of queries, not one each",
      len(reading_selects) <= 3, "%d selects: %s" % (len(reading_selects), reading_selects[:2]))

# ---------------------------------------------------------------- estimation
acct6 = new_account('avg3')
m6 = new_meter(acct6, 'P2-EST')
for day, val in (('01-31', 100.0), ('02-28', 250.0), ('03-31', 350.0)):
    read(m6, day, val).action_validate()
check("avg3 estimate", abs(acct6._estimate_consumption(m6) - (100 + 150 + 100) / 3) < 0.01,
      acct6._estimate_consumption(m6))
acct6.estimation_method = 'previous'
check("previous-period estimate", acct6._estimate_consumption(m6) == 100.0,
      acct6._estimate_consumption(m6))
acct6.estimation_method = 'minimum'
acct6.minimum_consumption = 42.0
check("minimum estimate", acct6._estimate_consumption(m6) == 42.0)
acct6.estimation_method = 'none'
check("do-not-estimate returns zero", acct6._estimate_consumption(m6) == 0.0)

acct6.estimation_method = 'avg3'
est = Reading._create_estimated_reading(m6, '2026-04-30 08:00:00')
check("estimated reading is typed and flagged",
      est.reading_type == 'estimated' and est.exception_state == 'estimated')
check("estimate keeps the dial series continuous",
      abs(est.present_reading - (350.0 + 350 / 3)) < 0.01, est.present_reading)

# ---------------------------------------------------------------- missing readings
missing = acct6._meters_missing_reading(
    fields.Date.to_date('2026-05-01'), fields.Date.to_date('2026-05-31'))
check("missing-reading check finds the unread meter", m6 in missing)

# ---------------------------------------------------------------- true-up
acct7 = new_account()
m7 = new_meter(acct7, 'P2-TRUEUP')
for day, val in (('01-31', 100.0), ('02-28', 200.0)):
    read(m7, day, val).action_validate()
over = read(m7, '03-31', 500.0, reading_type='estimated')   # over-estimated by 200
over.action_approve_exception()
over.action_validate()
inv1 = (over)._bill()
check("estimate was billed", over.state == 'billed' and len(inv1) == 1)
actual = read(m7, '04-30', 320.0)   # real dial is BELOW the estimate
check("true-up is not treated as an error", 'negative' not in (actual.exception_codes or ''),
      actual.exception_codes)
check("true-up is identified", 'trueup' in (actual.exception_codes or ''),
      actual.exception_codes)
actual.action_approve_exception()
actual.action_validate()
check("true-up links the billed estimate", over in actual.trueup_reading_ids)
inv2 = actual._bill()
check("true-up produces a credit, not a charge", inv2.amount_total < 0 or
      inv2.invoice_line_ids[0].quantity < 0,
      inv2.invoice_line_ids.mapped('quantity'))

print("\nP2 READING VALIDATION: %d PASS / %d FAIL" % (PASS, FAIL))
env.cr.rollback()
print("rolled back")
