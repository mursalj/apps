"""Customer notifications: channels, logging and the WhatsApp gate (SRS 22, 37).

    odoo shell -d <database> --no-http --logfile=/dev/null \
      < addons/utility_management/tests/p6_notifications_check.py

Rolls back at the end. No message leaves the building: email is queued (force_send=False)
and rolled back, and WhatsApp is unconfigured in this test, which is exactly the state
being asserted. Expect 15 PASS / 0 FAIL.
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
Notification = env['utility.notification']

partner = env['res.partner'].create({
    'name': 'P6 Customer', 'email': 'p6.customer@example.invalid', 'phone': '+252611000000'})
silent = env['res.partner'].create({'name': 'P6 Silent Customer'})
tariff = env['utility.tariff'].create({
    'name': 'P6 Flat', 'utility_type': 'gas', 'structure': 'flat', 'flat_rate': 1.0})
cycle = env.ref('utility_management.billing_cycle_monthly')


def make_account(customer):
    acct = env['utility.service.account'].create({
        'partner_id': customer.id, 'utility_type': 'gas', 'tariff_id': tariff.id,
        'billing_cycle_id': cycle.id})
    acct.action_activate()
    meter = env['utility.meter'].create({
        'name': 'P6-%s' % customer.id, 'utility_type': 'gas', 'digits': 6,
        'state': 'active'})
    env['utility.meter.installation'].create({
        'meter_id': meter.id, 'account_id': acct.id, 'date_installed': '2026-01-01'})
    return acct, meter


acct, meter = make_account(partner)
r = env['utility.meter.reading'].create({
    'meter_id': meter.id, 'reading_date': '2026-02-28 08:00:00', 'present_reading': 60.0})
r.action_validate()
invoice = r._bill()
# Post it first: a draft move has no name yet, and a bill is only announced once it is
# real — the notification subject quotes the invoice number.
invoice.action_post()

# ---------------------------------------------------------------- templates exist
for key in ('invoice_issued', 'payment_received', 'overdue', 'reading_reminder',
            'estimated_bill'):
    template = env.ref(
        env['utility.notification']._fields['template_key'] and
        {'invoice_issued': 'utility_management.mail_template_utility_invoice',
         'payment_received': 'utility_management.mail_template_utility_payment',
         'overdue': 'utility_management.mail_template_utility_overdue',
         'reading_reminder': 'utility_management.mail_template_utility_reading_reminder',
         'estimated_bill': 'utility_management.mail_template_utility_estimated'}[key],
        raise_if_not_found=False)
    if not template:
        check("template for %s exists" % key, False, 'missing')
        break
else:
    check("all five SRS 37 templates are installed", True)

# ---------------------------------------------------------------- email channel
check("email is on by default, WhatsApp is not",
      acct.notify_email and not acct.notify_whatsapp)
notes = acct.notify('invoice_issued', record=invoice)
check("one notification row per channel", len(notes) == 1, len(notes))
note = notes[0]
check("email notification is sent", note.state == 'sent', (note.state, note.error))
check("the log records the recipient", note.recipient == partner.email, note.recipient)
check("the log records the rendered subject", invoice.name in (note.subject or ''),
      note.subject)
check("the log points back at the source record",
      note.res_model == 'account.move' and note.res_id == invoice.id)
check("an outgoing mail was queued, not blasted",
      env['mail.mail'].search_count([('res_id', '=', invoice.id)]) >= 1)

# ---------------------------------------------------------------- missing address
acct_silent, meter_silent = make_account(silent)
skipped = acct_silent.notify('overdue')
check("a customer with no email is skipped, not failed", skipped.state == 'skipped',
      (skipped.state, skipped.error))
check("the reason is recorded", 'no email' in (skipped.error or '').lower(), skipped.error)

# ---------------------------------------------------------------- whatsapp gate
acct.notify_whatsapp = True
env['ir.config_parameter'].sudo().set_param('utility_management.whatsapp_token', '')
env['ir.config_parameter'].sudo().set_param('utility_management.whatsapp_phone_id', '')
notes = acct.notify('invoice_issued', record=invoice)
whatsapp = notes.filtered(lambda n: n.channel == 'whatsapp')
check("WhatsApp produces its own log row", len(whatsapp) == 1)
check("WhatsApp is inert until credentials are configured",
      whatsapp.state == 'skipped', (whatsapp.state, whatsapp.error))
check("the log says exactly what is missing",
      'whatsapp_token' in (whatsapp.error or ''), whatsapp.error)
check("no WhatsApp credentials are configured on this database",
      Notification._whatsapp_config() is None)

# ---------------------------------------------------------------- run notifications
run = env['utility.billing.run'].create({
    'period_start': '2026-03-01', 'period_end': '2026-03-31',
    'billing_cycle_id': cycle.id, 'utility_type': 'gas'})
r2 = env['utility.meter.reading'].create({
    'meter_id': meter.id, 'reading_date': '2026-03-31 08:00:00', 'present_reading': 110.0})
r2.action_validate()
run.action_compute()
run.line_ids.mapped('reading_id').filtered(
    lambda x: x.exception_state != 'normal').action_approve_exception()
run.action_compute()
run.action_approve()
run.action_generate_invoices()
try:
    with env.cr.savepoint():
        env['utility.billing.run'].create({
            'period_start': '2026-04-01', 'period_end': '2026-04-30'
        }).action_notify_customers()
    check("a run cannot notify before it has invoiced", False, "no error raised")
except Exception as e:
    check("a run cannot notify before it has invoiced", 'before notifying' in str(e)
          or 'Generate the invoices' in str(e), str(e)[:60])

before = Notification.search_count([])
run.action_notify_customers()
check("notifying a run logs a row per invoice and channel",
      Notification.search_count([]) > before)

print("\nP6 NOTIFICATIONS: %d PASS / %d FAIL" % (PASS, FAIL))
env.cr.rollback()
print("rolled back")
