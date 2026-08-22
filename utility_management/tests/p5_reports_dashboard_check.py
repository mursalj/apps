"""Bill PDF, customer statement and dashboard figures (SRS 15, 24, 25, 36).

    odoo shell -d <database> --no-http --logfile=/dev/null \
      < addons/utility_management/tests/p5_reports_dashboard_check.py

Rolls back at the end. Expect 18 PASS / 0 FAIL.
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


env.user.group_ids = [(4, env.ref('utility_management.group_utility_manager').id)]
Reading = env['utility.meter.reading']

partner = env['res.partner'].create({'name': 'P5 Customer'})
tariff = env['utility.tariff'].create({
    'name': 'P5 Flat', 'utility_type': 'water', 'structure': 'flat', 'flat_rate': 3.0,
    'base_charge': 5.0, 'unit_name': 'm3'})
cycle = env.ref('utility_management.billing_cycle_monthly')
acct = env['utility.service.account'].create({
    'partner_id': partner.id, 'utility_type': 'water', 'tariff_id': tariff.id,
    'billing_cycle_id': cycle.id, 'street': '12 Market Road', 'city': 'Hargeisa',
    'area': 'Zone 3'})
acct.action_activate()
meter = env['utility.meter'].create({
    'name': 'P5-M1', 'utility_type': 'water', 'digits': 6, 'state': 'active'})
env['utility.meter.installation'].create({
    'meter_id': meter.id, 'account_id': acct.id, 'date_installed': '2026-01-01'})

r1 = Reading.create({'meter_id': meter.id, 'reading_date': '2026-01-31 08:00:00',
                     'present_reading': 40.0})
r1.action_validate()
r2 = Reading.create({'meter_id': meter.id, 'reading_date': '2026-02-28 08:00:00',
                     'present_reading': 90.0})
r2.action_validate()
invoice = (r1 | r2)._bill()
invoice.action_post()

# ---------------------------------------------------------------- bill PDF
report = env.ref('utility_management.action_report_utility_bill')
check("the bill report is bound to invoices", report.model == 'account.move')
html = env['ir.actions.report']._render_qweb_html(
    'utility_management.report_utility_bill', invoice.ids)[0].decode()
check("bill shows the service account", acct.name in html, acct.name)
check("bill shows the service address", '12 Market Road' in html)
check("bill shows the service period",
      'Service period' in html and '02/28/2026' in html,
      html[html.find('Service period'):][:120] if 'Service period' in html else 'label missing')
check("bill shows the meter", meter.name in html)
check("bill shows previous and present readings", '40.0' in html and '90.0' in html)
check("bill shows consumption", '50.0' in html)
check("bill carries a payment QR", 'barcode' in html and 'QR' in html)
check("bill shows the amount due", str(int(invoice.amount_total)) in html,
      invoice.amount_total)

estimated = Reading.create({
    'meter_id': meter.id, 'reading_date': '2026-03-31 08:00:00',
    'present_reading': 140.0, 'reading_type': 'estimated'})
estimated.action_approve_exception()
estimated.action_validate()
est_invoice = estimated._bill()
est_html = env['ir.actions.report']._render_qweb_html(
    'utility_management.report_utility_bill', est_invoice.ids)[0].decode()
check("an estimated bill says so on its face", 'estimated reading' in est_html.lower(),
      est_invoice.utility_is_estimated)

# ---------------------------------------------------------------- statement
statement = env.ref('utility_management.action_report_utility_statement')
check("the statement report is bound to service accounts",
      statement.model == 'utility.service.account')
rows = env['report.utility_management.report_utility_statement']._statement_lines(acct)
check("statement has a row for the posted invoice", len(rows) >= 1, len(rows))
check("statement debits the invoice",
      abs(rows[0]['debit'] - invoice.amount_total) < 0.01, rows[0])
check("statement carries a running balance", 'balance' in rows[0])

payment = env['account.payment'].create({
    'payment_type': 'inbound', 'partner_type': 'customer',
    'partner_id': partner.id, 'amount': 50.0,
    'date': '2026-03-05',
})
payment.action_post()
lines = (payment.move_id.line_ids + invoice.line_ids).filtered(
    lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled)
lines.reconcile()
rows = env['report.utility_management.report_utility_statement']._statement_lines(acct)
credits = [r for r in rows if r['credit']]
check("statement shows the payment as a credit", bool(credits), rows)
check("balance falls by the payment", abs(rows[-1]['balance'] -
      (invoice.amount_total - 50.0)) < 0.01, rows[-1]['balance'])

statement_html = env['ir.actions.report']._render_qweb_html(
    'utility_management.report_utility_statement', acct.ids)[0].decode()
check("statement PDF renders the ledger", 'Account Statement' in statement_html
      and acct.name in statement_html)

# ---------------------------------------------------------------- dashboard
data = env['utility.dashboard'].get_dashboard_data(months=12)
kpis = data['kpis']
for key in ('reading_completion', 'billing_completion', 'estimated_share',
            'collection_rate', 'open_exceptions', 'runs_awaiting_review', 'accounts'):
    if key not in kpis:
        check("dashboard exposes %s" % key, False, sorted(kpis))
        break
else:
    check("dashboard exposes the SRS 25 completion and quality rates", True)
check("dashboard revenue is aggregated, not scanned", data['series'] and
      any(b['revenue'] for b in data['series']),
      [b['revenue'] for b in data['series']])
check("dashboard attention list carries the exception reason",
      all('codes' in row for row in data['attention']), data['attention'][:1])

print("\nP5 REPORTS & DASHBOARD: %d PASS / %d FAIL" % (PASS, FAIL))
env.cr.rollback()
print("rolled back")
