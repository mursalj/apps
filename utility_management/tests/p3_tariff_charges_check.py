"""Tariff structures, block bounds and configured charges (SRS 11, 13).

    odoo shell -d <database> --no-http --logfile=/dev/null \
      < addons/utility_management/tests/p3_tariff_charges_check.py

Rolls back at the end. Expect 22 PASS / 0 FAIL.
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


Tariff = env['utility.tariff']


def total(lines, kind=None):
    return sum(l['subtotal'] for l in lines if kind is None or l['kind'] == kind)


# ---------------------------------------------------------------- tiered maths
tiered = Tariff.create({
    'name': 'P3 Tiered', 'utility_type': 'electricity', 'structure': 'tiered',
    'unit_name': 'kWh',
    'block_ids': [
        (0, 0, {'sequence': 1, 'upper_limit': 100, 'rate': 0.10}),
        (0, 0, {'sequence': 2, 'upper_limit': 300, 'rate': 0.15}),
        (0, 0, {'sequence': 3, 'upper_limit': 500, 'rate': 0.20}),
        (0, 0, {'sequence': 4, 'upper_limit': 0, 'rate': 0.25}),
    ]})
# The SRS's own worked example: 0-100 @ .10, 101-300 @ .15, 301-500 @ .20, 501+ @ .25
lines = tiered.compute_charges(250)
check("marginal tiering charges each block at its own rate",
      abs(total(lines) - (100 * 0.10 + 150 * 0.15)) < 0.001, total(lines))
check("one line per block touched", len(lines) == 2, len(lines))
lines = tiered.compute_charges(600)
check("open-ended last block absorbs the remainder",
      abs(total(lines) - (100 * .10 + 200 * .15 + 200 * .20 + 100 * .25)) < 0.001,
      total(lines))
check("consumption inside the first block bills one line",
      len(tiered.compute_charges(50)) == 1)
check("zero consumption bills nothing", tiered.compute_charges(0) == [])
check("lower_limit is derived, not typed",
      [b.lower_limit for b in tiered.block_ids.sorted('sequence')] == [0, 100, 300, 500],
      [b.lower_limit for b in tiered.block_ids.sorted('sequence')])

# ---------------------------------------------------------------- block bound guards
try:
    with env.cr.savepoint():
        Tariff.create({
            'name': 'P3 Bad Order', 'utility_type': 'water', 'structure': 'tiered',
            'block_ids': [(0, 0, {'sequence': 1, 'upper_limit': 300, 'rate': 1}),
                          (0, 0, {'sequence': 2, 'upper_limit': 100, 'rate': 2})]})
    check("descending blocks refused", False, "no error raised")
except Exception as e:
    check("descending blocks refused", 'must increase' in str(e), str(e)[:60])

try:
    with env.cr.savepoint():
        Tariff.create({
            'name': 'P3 Bad Open', 'utility_type': 'water', 'structure': 'tiered',
            'block_ids': [(0, 0, {'sequence': 1, 'upper_limit': 0, 'rate': 1}),
                          (0, 0, {'sequence': 2, 'upper_limit': 100, 'rate': 2})]})
    check("only the last block may be open-ended", False, "no error raised")
except Exception as e:
    check("only the last block may be open-ended", 'LAST block' in str(e), str(e)[:60])

# The old bug: a middle block ending at 0 was read as infinity and swallowed everything.
single_open = Tariff.create({
    'name': 'P3 Single Open', 'utility_type': 'gas', 'structure': 'tiered',
    'block_ids': [(0, 0, {'sequence': 1, 'upper_limit': 0, 'rate': 2.0})]})
check("a lone open-ended block bills everything at its rate",
      abs(total(single_open.compute_charges(70)) - 140.0) < 0.001,
      total(single_open.compute_charges(70)))

# ---------------------------------------------------------------- flat + base
flat = Tariff.create({
    'name': 'P3 Flat', 'utility_type': 'water', 'structure': 'flat', 'flat_rate': 2.0,
    'base_charge': 15.0, 'unit_name': 'm3'})
lines = flat.compute_charges(30)
check("flat usage", abs(total(lines, 'usage') - 60.0) < 0.001, total(lines, 'usage'))
check("base charge is added once, unscaled", total(lines, 'base') == 15.0)
check("base charge applies even at zero usage",
      total(flat.compute_charges(0), 'base') == 15.0)

# ---------------------------------------------------------------- configured charges
fees = Tariff.create({
    'name': 'P3 Charges', 'utility_type': 'electricity', 'structure': 'flat',
    'flat_rate': 1.0, 'unit_name': 'kWh',
    'charge_ids': [
        (0, 0, {'sequence': 10, 'name': 'Meter rent', 'kind': 'fee',
                'computation': 'fixed', 'amount': 5.0}),
        (0, 0, {'sequence': 20, 'name': 'Environmental levy', 'kind': 'fee',
                'computation': 'per_unit', 'amount': 0.02}),
        (0, 0, {'sequence': 30, 'name': 'Prompt payment discount', 'kind': 'discount',
                'computation': 'percent', 'amount': 10.0}),
    ]})
lines = fees.compute_charges(100)
check("fixed fee applied", total(lines, 'fee') == 5.0 + 2.0, total(lines, 'fee'))
check("discount is negative", total(lines, 'discount') < 0, total(lines, 'discount'))
check("discount is a percentage of the charges so far",
      abs(total(lines, 'discount') + (100 + 5 + 2) * 0.10) < 0.001,
      total(lines, 'discount'))

minimum = Tariff.create({
    'name': 'P3 Minimum', 'utility_type': 'water', 'structure': 'flat', 'flat_rate': 1.0,
    'charge_ids': [(0, 0, {'name': 'Minimum monthly charge', 'kind': 'minimum',
                           'computation': 'fixed', 'amount': 50.0})]})
check("minimum tops a small bill up", abs(total(minimum.compute_charges(10)) - 50.0) < 0.001,
      total(minimum.compute_charges(10)))
check("minimum leaves a larger bill alone",
      abs(total(minimum.compute_charges(80)) - 80.0) < 0.001,
      total(minimum.compute_charges(80)))

maximum = Tariff.create({
    'name': 'P3 Maximum', 'utility_type': 'water', 'structure': 'flat', 'flat_rate': 1.0,
    'charge_ids': [(0, 0, {'name': 'Capped charge', 'kind': 'maximum',
                           'computation': 'fixed', 'amount': 40.0})]})
check("maximum caps a large bill", abs(total(maximum.compute_charges(100)) - 40.0) < 0.001,
      total(maximum.compute_charges(100)))

penalty = Tariff.create({
    'name': 'P3 Penalty', 'utility_type': 'electricity', 'structure': 'flat',
    'flat_rate': 1.0,
    'charge_ids': [(0, 0, {'name': 'Late payment penalty', 'kind': 'penalty',
                           'computation': 'percent', 'amount': 5.0})]})
check("penalties are off unless the run asks for them",
      total(penalty.compute_charges(100), 'penalty') == 0.0)
check("penalty charged on the overdue balance, not on this month's usage",
      abs(total(penalty.compute_charges(100, apply_penalties=True, overdue_amount=200.0),
                'penalty') - 10.0) < 0.001,
      total(penalty.compute_charges(100, apply_penalties=True, overdue_amount=200.0),
            'penalty'))

threshold = Tariff.create({
    'name': 'P3 Threshold', 'utility_type': 'gas', 'structure': 'flat', 'flat_rate': 1.0,
    'charge_ids': [(0, 0, {'name': 'Heavy user surcharge', 'kind': 'fee',
                           'computation': 'fixed', 'amount': 25.0,
                           'apply_when': 'usage_above', 'threshold': 500.0})]})
check("conditional charge stays off below its threshold",
      total(threshold.compute_charges(100), 'fee') == 0.0)
check("conditional charge fires above its threshold",
      total(threshold.compute_charges(900), 'fee') == 25.0)

try:
    with env.cr.savepoint():
        env['utility.tariff.charge'].create({
            'tariff_id': flat.id, 'name': 'Bad minimum', 'kind': 'minimum',
            'computation': 'percent', 'amount': 10.0})
    check("a percentage minimum is refused", False, "no error raised")
except Exception as e:
    check("a percentage minimum is refused", 'fixed amount' in str(e), str(e)[:60])

# ---------------------------------------------------------------- effective dates
dated = Tariff.create({
    'name': 'P3 Seasonal', 'utility_type': 'electricity', 'structure': 'flat',
    'flat_rate': 1.0, 'date_start': '2026-06-01', 'date_end': '2026-08-31'})
from odoo import fields as odoo_fields
check("tariff is effective inside its window",
      dated._is_effective_on(odoo_fields.Date.to_date('2026-07-01')))
check("tariff is not effective before it starts",
      not dated._is_effective_on(odoo_fields.Date.to_date('2026-05-31')))

print("\nP3 TARIFF CHARGES: %d PASS / %d FAIL" % (PASS, FAIL))
env.cr.rollback()
print("rolled back")
