# -*- coding: utf-8 -*-
"""Compliance records (PRD 5, 6, 70, 71, 72, 73).

What an inspector asks for, and what the module could not produce:

* **Which pharmacy is this?** A licence number, its expiry, and the pharmacist legally
  responsible. Odoo has a company; it does not have a pharmacy licence.
* **Who made this?** Manufacturers were a checkbox on a contact. A manufacturer has a
  registration, a country and an approval decision behind it.
* **Where are the papers?** Product registrations, certificates of analysis, import
  permits - documents with an issuing authority and an expiry date that somebody has to
  be told about BEFORE it lapses.
* **Who authorised that?** The module already refuses things (expired stock, unapproved
  suppliers, credit holds, clinical warnings) and lets an authorised person override
  some of them. Those overrides were scattered across chatter messages. A regulator
  wants one list.

None of this duplicates Odoo: documents are ir.attachment underneath, the profile hangs
off res.company, and the override log is a plain record with a reason.
"""

import logging

from dateutil.relativedelta import relativedelta

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

DOCUMENT_TYPES = [
    ('licence', 'Pharmacy Licence'),
    ('registration', 'Product Registration'),
    ('coa', 'Certificate of Analysis'),
    ('import_permit', 'Import Permit'),
    ('gmp', 'GMP Certificate'),
    ('insurance', 'Insurance Policy'),
    ('tax', 'Tax Certificate'),
    ('other', 'Other'),
]

OVERRIDE_KINDS = [
    ('expired_stock', 'Sold or Dispensed Expired Stock'),
    ('clinical', 'Proceeded Past a Clinical Warning'),
    ('credit', 'Confirmed an Order Over the Credit Limit'),
    ('supplier', 'Bought From an Unapproved Supplier'),
    ('wholesale', 'Confirmed a Blocked Wholesale Order'),
    ('price', 'Changed a Price'),
    ('other', 'Other'),
]


class PharmacyProfile(models.Model):
    _name = 'pharmacy.profile'
    _description = 'Pharmacy'
    _inherit = ['mail.thread']
    _order = 'name'

    name = fields.Char('Pharmacy Name', required=True, tracking=True)
    legal_name = fields.Char('Legal Name')
    company_id = fields.Many2one('res.company', string='Company', required=True,
                                 default=lambda s: s.env.company)
    branch_code = fields.Char('Branch Code', help='Short code used in reports and on '
                                                  'labels for a chain.')

    registration_no = fields.Char('Registration No.')
    licence_no = fields.Char('Pharmacy Licence No.', tracking=True)
    licence_authority = fields.Char('Issuing Authority')
    licence_expiry = fields.Date('Licence Expires', tracking=True)
    licence_state = fields.Selection([
        ('none', 'Not Recorded'),
        ('valid', 'Valid'),
        ('expiring', 'Expiring Soon'),
        ('expired', 'Expired'),
    ], compute='_compute_licence_state', store=True, string='Licence Status')
    tax_id_no = fields.Char('Tax ID')

    pharmacist_in_charge_id = fields.Many2one(
        'res.users', string='Pharmacist in Charge', tracking=True,
        help='The pharmacist legally answerable for this branch.')
    pharmacist_licence_no = fields.Char('Pharmacist Licence No.')

    street = fields.Char('Address')
    city = fields.Char('City')
    phone = fields.Char('Phone')
    email = fields.Char('Email')
    website = fields.Char('Website')
    opening_hours = fields.Text('Operating Hours')

    warehouse_id = fields.Many2one('stock.warehouse', string='Default Warehouse')
    pos_config_id = fields.Many2one('pos.config', string='Default Point of Sale',
                                    domain="[('is_pharmacy', '=', True)]")
    document_ids = fields.One2many('pharmacy.document', 'profile_id',
                                   string='Documents')
    active = fields.Boolean(default=True)

    @api.depends('licence_expiry')
    def _compute_licence_state(self):
        today = fields.Date.today()
        horizon = today + relativedelta(
            days=self.env['res.partner']._licence_alert_days())
        for record in self:
            expiry = record.licence_expiry
            if not expiry:
                record.licence_state = 'none'
            elif expiry < today:
                record.licence_state = 'expired'
            elif expiry <= horizon:
                record.licence_state = 'expiring'
            else:
                record.licence_state = 'valid'


class PharmacyManufacturer(models.Model):
    """A manufacturer is a licensed entity, not a tick-box on a contact (PRD 70)."""
    _name = 'pharmacy.manufacturer'
    _description = 'Medicine Manufacturer'
    _inherit = ['mail.thread']
    _order = 'name'

    name = fields.Char('Manufacturer', required=True)
    partner_id = fields.Many2one('res.partner', string='Contact',
                                 help='Their record in Contacts, when they are also a '
                                      'supplier this pharmacy buys from.')
    country_id = fields.Many2one('res.country', string='Country')
    registration_no = fields.Char('Registration No.')
    gmp_certificate_no = fields.Char('GMP Certificate No.')
    gmp_expiry = fields.Date('GMP Certificate Expires')
    is_approved = fields.Boolean('Approved', tracking=True)
    approved_by_id = fields.Many2one('res.users', string='Approved By', readonly=True)
    approved_on = fields.Datetime('Approved On', readonly=True)
    website = fields.Char('Website')
    note = fields.Text('Notes')
    active = fields.Boolean(default=True)
    product_ids = fields.One2many('product.template', 'pharma_manufacturer_id',
                                  string='Products')
    product_count = fields.Integer(compute='_compute_product_count')
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company)

    def _compute_product_count(self):
        counts = {m.id: c for m, c in self.env['product.template']._read_group(
            [('pharma_manufacturer_id', 'in', self.ids)],
            ['pharma_manufacturer_id'], ['__count'])}
        for record in self:
            record.product_count = counts.get(record.id, 0)

    def action_approve(self):
        if not self.env.user.has_group('sahal_pharmacy.group_pharmacy_manager'):
            raise UserError(_("Approving a manufacturer is a Pharmacy Manager decision."))
        for record in self:
            record.write({
                'is_approved': True,
                'approved_by_id': self.env.user.id,
                'approved_on': fields.Datetime.now(),
            })

    def action_view_products(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Products by %s', self.name),
            'res_model': 'product.template',
            'view_mode': 'list,form',
            'domain': [('pharma_manufacturer_id', '=', self.id)],
        }


class PharmacyDocument(models.Model):
    """Regulatory paperwork with an expiry somebody must be warned about (PRD 71)."""
    _name = 'pharmacy.document'
    _description = 'Regulatory Document'
    _inherit = ['mail.thread']
    _order = 'expiry_date, id desc'

    name = fields.Char('Document', required=True)
    document_type = fields.Selection(DOCUMENT_TYPES, required=True, default='other')
    reference = fields.Char('Document Number')
    issuing_authority = fields.Char('Issuing Authority')
    issue_date = fields.Date('Issued On')
    expiry_date = fields.Date('Expires On', tracking=True)
    state = fields.Selection([
        ('none', 'No Expiry'),
        ('valid', 'Valid'),
        ('expiring', 'Expiring Soon'),
        ('expired', 'Expired'),
    ], compute='_compute_state', store=True)

    profile_id = fields.Many2one('pharmacy.profile', string='Pharmacy')
    partner_id = fields.Many2one('res.partner', string='Supplier / Customer')
    manufacturer_id = fields.Many2one('pharmacy.manufacturer', string='Manufacturer')
    product_id = fields.Many2one('product.template', string='Product')
    attachment_ids = fields.Many2many('ir.attachment', string='Files')
    note = fields.Text('Notes')
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company)

    @api.depends('expiry_date')
    def _compute_state(self):
        today = fields.Date.today()
        horizon = today + relativedelta(
            days=self.env['res.partner']._licence_alert_days())
        for record in self:
            expiry = record.expiry_date
            if not expiry:
                record.state = 'none'
            elif expiry < today:
                record.state = 'expired'
            elif expiry <= horizon:
                record.state = 'expiring'
            else:
                record.state = 'valid'

    @api.constrains('issue_date', 'expiry_date')
    def _check_dates(self):
        for record in self:
            if record.issue_date and record.expiry_date \
                    and record.expiry_date < record.issue_date:
                raise ValidationError(_(
                    "'%s' expires before it was issued.", record.name))

    @api.model
    def _cron_document_expiry_alert(self):
        """Warn about paperwork about to lapse. A report, never an automatic block."""
        today = fields.Date.today()
        horizon = today + relativedelta(
            days=self.env['res.partner']._licence_alert_days())
        at_risk = self.sudo().search([
            ('expiry_date', '!=', False), ('expiry_date', '<=', horizon),
        ], order='expiry_date')
        for document in at_risk:
            document.message_post(body=_(
                "%(type)s %(ref)s %(state)s on %(date)s.",
                type=dict(DOCUMENT_TYPES)[document.document_type],
                ref=document.reference or document.name,
                state=_('expired') if document.expiry_date < today else _('expires'),
                date=document.expiry_date))
        if at_risk:
            _logger.warning("Pharmacy documents expiring or expired: %s",
                            ', '.join(at_risk.mapped('name')))
        return len(at_risk)


class PharmacyOverride(models.Model):
    """One list of every time somebody was allowed past a control (PRD 73).

    The individual refusals already write to their own record's chatter. This exists so
    the question "show me every override last month, and who authorised it" has one
    answer instead of a search across five models.
    """
    _name = 'pharmacy.override'
    _description = 'Authorised Override'

    _order = 'create_date desc'

    kind = fields.Selection(OVERRIDE_KINDS, required=True)
    user_id = fields.Many2one('res.users', string='Authorised By', required=True,
                              default=lambda s: s.env.user, readonly=True)
    override_date = fields.Datetime('When', default=fields.Datetime.now, readonly=True)
    reason = fields.Text('Reason', required=True)
    res_model = fields.Char('Document Model', readonly=True)
    res_id = fields.Integer('Document', readonly=True)
    document_name = fields.Char('Reference', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Customer / Supplier')
    product_id = fields.Many2one('product.product', string='Product')
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company)

    @api.model
    def log(self, kind, reason, record=None, partner=None, product=None):
        """Record an override. Called by whatever let the user through."""
        values = {
            'kind': kind,
            'reason': reason,
            'partner_id': partner.id if partner else False,
            'product_id': product.id if product else False,
        }
        if record is not None and getattr(record, 'id', False):
            values.update({
                'res_model': record._name,
                'res_id': record.id,
                'document_name': record.display_name,
            })
        return self.sudo().create(values)

    def action_open_document(self):
        self.ensure_one()
        if not self.res_model or not self.res_id:
            raise UserError(_("This override is not attached to a document."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': self.res_model,
            'res_id': self.res_id,
            'view_mode': 'form',
        }


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    pharma_manufacturer_id = fields.Many2one(
        'pharmacy.manufacturer', string='Manufacturer (Licensed)',
        help='The licensed manufacturer record, as opposed to the free contact link.')
