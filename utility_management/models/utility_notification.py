# -*- coding: utf-8 -*-
"""Customer notifications (SRS 22, 37).

Every message the platform sends a customer goes through here and leaves a row behind:
which account, which channel, which template, what was sent, whether it left the building.
Without that log, "we told them" is an assertion; with it, it is a record — which is what
a billing dispute actually turns on.

Two channels are implemented:

* **email** — a real mail.template on the SMTP already configured for the platform. Works
  today, no vendor to sign up with.
* **whatsapp** — the Meta WhatsApp Cloud API. It stays INERT until credentials are
  configured (see _whatsapp_config): with none set, a WhatsApp notification is logged as
  'skipped' with the reason, never silently dropped and never half-sent. The
  odoo_whatsapp_integration addon already in this codebase cannot do this job: it builds
  click-to-chat links for a human to press, which is fine for an agent phoning a customer
  and useless for a billing run.

Nothing here sends automatically on a schedule. On this platform, customer-contacting
crons ship disabled and are turned on deliberately (docs/PROD_PENDING_CHANGES.md, cron
state) — the same rule applies to utility notifications.
"""

import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Template key -> the mail.template xml id it renders. Keys are stable; the templates
# behind them are a utility's to edit.
TEMPLATE_KEYS = {
    'invoice_issued': 'utility_management.mail_template_utility_invoice',
    'payment_received': 'utility_management.mail_template_utility_payment',
    'overdue': 'utility_management.mail_template_utility_overdue',
    'reading_reminder': 'utility_management.mail_template_utility_reading_reminder',
    'estimated_bill': 'utility_management.mail_template_utility_estimated',
}


class UtilityNotification(models.Model):
    _name = 'utility.notification'
    _description = 'Utility Customer Notification'
    _order = 'create_date desc, id desc'

    account_id = fields.Many2one('utility.service.account', string='Service Account',
                                 required=True, ondelete='cascade', index=True)
    partner_id = fields.Many2one(related='account_id.partner_id', store=True, readonly=True)
    template_key = fields.Selection([(k, k.replace('_', ' ').title())
                                     for k in TEMPLATE_KEYS], required=True)
    channel = fields.Selection([
        ('email', 'Email'),
        ('whatsapp', 'WhatsApp'),
    ], required=True)
    state = fields.Selection([
        ('pending', 'Pending'),
        ('sent', 'Sent'),
        ('skipped', 'Skipped'),
        ('failed', 'Failed'),
    ], default='pending', required=True, index=True)
    recipient = fields.Char('Sent To')
    subject = fields.Char('Subject')
    body = fields.Text('Message')
    error = fields.Char('Reason', help='Why the message was skipped or failed.')
    sent_on = fields.Datetime('Sent On')
    res_model = fields.Char('Source Model')
    res_id = fields.Integer('Source Record')
    company_id = fields.Many2one(related='account_id.company_id', store=True, readonly=True)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    @api.model
    def notify(self, account, template_key, record=None, channels=None):
        """Send one notification per channel the customer has opted into.

        Returns the notification records. A channel that cannot send (no address, no
        credentials) produces a 'skipped' row rather than an exception: one customer with
        a missing phone number must not abort a billing run's notifications for everyone
        else.
        """
        if template_key not in TEMPLATE_KEYS:
            raise UserError(_("Unknown notification template '%s'.", template_key))
        record = record or account
        channels = channels or account._notification_channels()
        notifications = self.browse()
        for channel in channels:
            notification = self.create({
                'account_id': account.id,
                'template_key': template_key,
                'channel': channel,
                'res_model': record._name,
                'res_id': record.id,
            })
            try:
                getattr(notification, '_send_%s' % channel)(record)
            except Exception as exc:               # noqa: BLE001 - logged, never raised on
                _logger.warning("Utility notification %s failed: %s", notification.id, exc)
                notification.write({'state': 'failed', 'error': str(exc)[:250]})
            notifications |= notification
        return notifications

    def _template(self):
        self.ensure_one()
        return self.env.ref(TEMPLATE_KEYS[self.template_key], raise_if_not_found=False)

    def _send_email(self, record):
        self.ensure_one()
        partner = self.account_id.partner_id
        if not partner.email:
            self.write({'state': 'skipped',
                        'error': _("The customer has no email address.")})
            return
        template = self._template()
        if not template:
            self.write({'state': 'skipped',
                        'error': _("Mail template is missing from the database.")})
            return
        rendered = template._render_field('subject', [record.id])[record.id]
        body = template._render_field('body_html', [record.id])[record.id]
        template.send_mail(record.id, email_values={'email_to': partner.email},
                           force_send=False)
        self.write({'state': 'sent', 'recipient': partner.email, 'subject': rendered,
                    'body': body, 'sent_on': fields.Datetime.now()})

    def _send_whatsapp(self, record):
        self.ensure_one()
        config = self._whatsapp_config()
        number = self.account_id._notification_phone()
        if not number:
            self.write({'state': 'skipped',
                        'error': _("The customer has no phone number.")})
            return
        if not config:
            # The honest outcome: recorded, visible, and obviously not delivered.
            self.write({
                'state': 'skipped',
                'recipient': number,
                'error': _("WhatsApp is not configured. Set utility_management."
                           "whatsapp_token and .whatsapp_phone_id (Meta Cloud API) to "
                           "enable it."),
            })
            return
        template = self._template()
        body = template._render_field('body_html', [record.id])[record.id] if template else ''
        self._whatsapp_post(config, number, body)
        self.write({'state': 'sent', 'recipient': number, 'body': body,
                    'sent_on': fields.Datetime.now()})

    @api.model
    def _whatsapp_config(self):
        """Meta Cloud API credentials, or None when WhatsApp is not set up."""
        params = self.env['ir.config_parameter'].sudo()
        token = params.get_param('utility_management.whatsapp_token')
        phone_id = params.get_param('utility_management.whatsapp_phone_id')
        if not token or not phone_id:
            return None
        return {
            'token': token,
            'phone_id': phone_id,
            'url': params.get_param('utility_management.whatsapp_api_url',
                                    'https://graph.facebook.com/v20.0'),
            'template_name': params.get_param(
                'utility_management.whatsapp_template_name'),
            'language': params.get_param('utility_management.whatsapp_language', 'en'),
        }

    def _whatsapp_post(self, config, number, body):
        """One HTTP call to the Cloud API.

        Meta only allows a free-form message inside a 24-hour customer service window; a
        billing notification is almost always outside it, so a PRE-APPROVED template is
        used when one is configured. Both shapes are here because a utility that has not
        yet had a template approved still needs the plumbing to be testable.
        """
        self.ensure_one()
        import requests
        from odoo.tools import html2plaintext

        text = html2plaintext(body or '')[:1024]
        if config.get('template_name'):
            payload = {
                'messaging_product': 'whatsapp',
                'to': number,
                'type': 'template',
                'template': {
                    'name': config['template_name'],
                    'language': {'code': config['language']},
                    'components': [{'type': 'body',
                                    'parameters': [{'type': 'text', 'text': text}]}],
                },
            }
        else:
            payload = {'messaging_product': 'whatsapp', 'to': number, 'type': 'text',
                       'text': {'body': text}}
        response = requests.post(
            '%s/%s/messages' % (config['url'].rstrip('/'), config['phone_id']),
            json=payload,
            headers={'Authorization': 'Bearer %s' % config['token']},
            timeout=20)
        if response.status_code >= 300:
            raise UserError(_("WhatsApp API returned %(code)s: %(body)s",
                              code=response.status_code, body=response.text[:200]))
