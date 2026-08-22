# -*- coding: utf-8 -*-
"""Customer returns (PRD 45).

"Returns must be more controlled than normal retail" - because the thing coming back is
medicine, and the question is not whether to refund but whether it may ever be sold to
anyone else. A blouse that comes back goes on the rail. A box of antibiotics that has
been out of the pharmacy's custody has been somewhere unknown, at an unknown
temperature, in unknown hands.

So this model separates two decisions that ordinary retail treats as one:

    1. Do we refund the customer?         -> a commercial decision
    2. Does the stock go back on sale?    -> a pharmacist's decision, defaulting to NO

`restock` defaults to false and cannot be set for anything opened, damaged, expired or
recalled. The refund is independent: a customer can be refunded for medicine that is
then destroyed, which is the normal outcome.
"""

import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

RETURN_REASONS = [
    ('wrong_medicine', 'Wrong Medicine Supplied'),
    ('wrong_quantity', 'Wrong Quantity'),
    ('damaged', 'Damaged'),
    ('changed_mind', 'Customer Changed Mind'),
    ('recall', 'Recall'),
    ('prescription_issue', 'Prescription Cancelled or Changed'),
    ('adverse_reaction', 'Adverse Reaction'),
    ('other', 'Other'),
]

CONDITIONS = [
    ('sealed', 'Sealed / Unopened'),
    ('opened', 'Opened'),
    ('damaged', 'Damaged'),
    ('expired', 'Expired'),
    ('unknown', 'Unknown'),
]

# The only condition in which returned medicine may go back on the shelf at all.
RESTOCKABLE_CONDITIONS = ('sealed',)


class PharmacyReturn(models.Model):
    _name = 'pharmacy.return'
    _description = 'Customer Medicine Return'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'return_date desc, id desc'

    name = fields.Char('Reference', copy=False, readonly=True, index=True,
                       default=lambda s: _('New'))
    partner_id = fields.Many2one('res.partner', string='Customer', required=True,
                                 tracking=True)
    return_date = fields.Datetime('Returned On', required=True, tracking=True,
                                  default=fields.Datetime.now)
    reason = fields.Selection(RETURN_REASONS, required=True, tracking=True)
    reason_note = fields.Text('Details')

    dispense_id = fields.Many2one('pharmacy.dispense', string='Original Dispensing',
                                  help='When the medicine was dispensed against a '
                                       'prescription, link it so the refill count and '
                                       'the patient record stay honest.')
    sale_order_id = fields.Many2one('sale.order', string='Original Sale')
    invoice_id = fields.Many2one('account.move', string='Original Invoice',
                                 domain="[('move_type', '=', 'out_invoice')]")

    line_ids = fields.One2many('pharmacy.return.line', 'return_id', string='Lines')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('approved', 'Approved'),
        ('done', 'Processed'),
        ('refunded', 'Refunded'),
        ('cancel', 'Cancelled'),
    ], default='draft', required=True, tracking=True)

    pharmacist_id = fields.Many2one('res.users', string='Pharmacist', readonly=True,
                                    copy=False, tracking=True)
    approved_on = fields.Datetime('Approved On', readonly=True, copy=False)
    credit_note_id = fields.Many2one('account.move', string='Credit Note', readonly=True,
                                     copy=False)
    picking_id = fields.Many2one('stock.picking', string='Stock Return', readonly=True,
                                 copy=False)

    restock_count = fields.Integer('Lines Restocked', compute='_compute_disposition',
                                   store=True)
    destroy_count = fields.Integer('Lines Destroyed', compute='_compute_disposition',
                                   store=True)
    refund_amount = fields.Monetary('Refund', compute='_compute_disposition', store=True,
                                    currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id',
                                  readonly=True)
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company,
                                 required=True)

    @api.depends('line_ids.restock', 'line_ids.quantity', 'line_ids.unit_price',
                 'line_ids.refund')
    def _compute_disposition(self):
        for record in self:
            record.restock_count = len(record.line_ids.filtered('restock'))
            record.destroy_count = len(record.line_ids.filtered(lambda l: not l.restock))
            record.refund_amount = sum(
                line.quantity * line.unit_price
                for line in record.line_ids.filtered('refund'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'pharmacy.return') or _('New')
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def action_approve(self):
        """A pharmacist accepts the return and rules on each line's disposition."""
        if not self.env.user.has_group('sahal_pharmacy.group_pharmacy_pharmacist'):
            raise UserError(_(
                "Accepting returned medicine is a pharmacist's decision: someone has to "
                "judge whether it can ever be sold again."))
        for record in self:
            if record.state != 'draft':
                raise UserError(_("Only a draft return can be approved."))
            if not record.line_ids:
                raise UserError(_("Add what is being returned first."))
            record.write({
                'state': 'approved',
                'pharmacist_id': self.env.user.id,
                'approved_on': fields.Datetime.now(),
            })
            record.message_post(body=_(
                "Return accepted: %(restock)s line(s) back to stock, %(destroy)s for "
                "destruction.", restock=record.restock_count,
                destroy=record.destroy_count))

    def action_process(self):
        """Move the stock: restockable lines back in, the rest scrapped."""
        self.ensure_one()
        if self.state != 'approved':
            raise UserError(_("Approve the return before processing it."))

        restock_lines = self.line_ids.filtered('restock')
        if restock_lines:
            self.picking_id = self._create_return_picking(restock_lines)

        # Everything else is scrapped, on the record, with the reason attached. Odoo's
        # own scrap is used rather than a bespoke write-off so the stock valuation and
        # the inventory report stay correct.
        for line in self.line_ids.filtered(lambda l: not l.restock):
            self.env['stock.scrap'].create({
                'product_id': line.product_id.id,
                'scrap_qty': line.quantity,
                'product_uom_id': line.product_id.uom_id.id,
                'lot_id': line.lot_id.id or False,
                'origin': self.name,
                'company_id': self.company_id.id,
            })
        self.state = 'done'
        self.message_post(body=_(
            "Processed: %(restock)s line(s) returned to stock, %(destroy)s scrapped.",
            restock=len(restock_lines), destroy=self.destroy_count))
        return True

    def _create_return_picking(self, lines):
        """An incoming transfer bringing the sealed stock back to the shelf."""
        self.ensure_one()
        picking_type = self.env['stock.picking.type'].search([
            ('code', '=', 'incoming'), ('company_id', '=', self.company_id.id),
        ], limit=1)
        if not picking_type:
            raise UserError(_("No incoming operation type is configured for %s.",
                              self.company_id.name))
        customer_location = self.env.ref('stock.stock_location_customers')
        moves = [(0, 0, {
            'description_picking': _('Return: %s', line.product_id.display_name),
            'product_id': line.product_id.id,
            'product_uom_qty': line.quantity,
            'product_uom': line.product_id.uom_id.id,
            'location_id': customer_location.id,
            'location_dest_id': picking_type.default_location_dest_id.id,
        }) for line in lines]
        picking = self.env['stock.picking'].create({
            'partner_id': self.partner_id.id,
            'picking_type_id': picking_type.id,
            'location_id': customer_location.id,
            'location_dest_id': picking_type.default_location_dest_id.id,
            'origin': self.name,
            'company_id': self.company_id.id,
            'move_ids': moves,
        })
        for move, line in zip(picking.move_ids, lines):
            if line.lot_id:
                move.move_line_ids = [(0, 0, {
                    'product_id': line.product_id.id,
                    'lot_id': line.lot_id.id,
                    'quantity': line.quantity,
                    'location_id': move.location_id.id,
                    'location_dest_id': move.location_dest_id.id,
                })]
        picking.action_confirm()
        return picking

    def action_refund(self):
        """Raise the customer credit note for the lines marked refundable."""
        self.ensure_one()
        if self.credit_note_id:
            raise UserError(_("Credit note %s already exists for this return.",
                              self.credit_note_id.name or self.credit_note_id.id))
        if self.state not in ('approved', 'done'):
            raise UserError(_("Approve the return before refunding it."))
        refundable = self.line_ids.filtered('refund')
        if not refundable:
            raise UserError(_(
                "No line on this return is marked for refund. A return can be accepted "
                "without a refund - untick nothing and there is nothing to credit."))
        lines = [(0, 0, {
            'product_id': line.product_id.id,
            'name': _('%(product)s - returned (%(reason)s)',
                      product=line.product_id.display_name,
                      reason=dict(RETURN_REASONS)[self.reason]),
            'quantity': line.quantity,
            'price_unit': line.unit_price,
        }) for line in refundable]
        move = self.env['account.move'].with_company(self.company_id).create({
            'move_type': 'out_refund',
            'partner_id': self.partner_id.id,
            'invoice_date': fields.Date.context_today(self),
            'invoice_origin': self.name,
            'company_id': self.company_id.id,
            'invoice_line_ids': lines,
        })
        self.write({'credit_note_id': move.id, 'state': 'refunded'})
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': move.id,
        }

    def action_cancel(self):
        for record in self:
            if record.picking_id and record.picking_id.state == 'done':
                raise UserError(_(
                    "Stock has already been taken back on %s. Reverse that transfer "
                    "first.", record.picking_id.name))
            record.state = 'cancel'

    def action_reset_draft(self):
        for record in self:
            if record.state in ('done', 'refunded'):
                raise UserError(_(
                    "%s has been processed. Reverse the transfer or the credit note "
                    "instead of reopening it.", record.name))
            record.write({'state': 'draft', 'pharmacist_id': False,
                          'approved_on': False})


class PharmacyReturnLine(models.Model):
    _name = 'pharmacy.return.line'
    _description = 'Customer Medicine Return Line'


    return_id = fields.Many2one('pharmacy.return', required=True, ondelete='cascade',
                                index=True)
    product_id = fields.Many2one('product.product', string='Product', required=True)
    lot_id = fields.Many2one('stock.lot', string='Batch',
                             domain="[('product_id', '=', product_id)]")
    expiration_date = fields.Datetime(related='lot_id.expiration_date', readonly=True)
    quantity = fields.Float('Quantity', required=True, default=1.0)
    unit_price = fields.Float('Unit Price')
    condition = fields.Selection(CONDITIONS, required=True, default='unknown',
                                 tracking=True)
    restock = fields.Boolean(
        'Return to Saleable Stock', default=False,
        help='Off by default. Medicine that has left the pharmacy has been in unknown '
             'conditions; only a sealed, in-date pack may go back, and only because a '
             'pharmacist says so.')
    refund = fields.Boolean('Refund This Line', default=True)
    note = fields.Char('Note')
    company_id = fields.Many2one(related='return_id.company_id', store=True,
                                 readonly=True)

    @api.constrains('restock', 'condition', 'lot_id')
    def _check_restockable(self):
        """The rule the whole model exists for."""
        now = fields.Datetime.now()
        for line in self:
            if not line.restock:
                continue
            if line.condition not in RESTOCKABLE_CONDITIONS:
                raise ValidationError(_(
                    "%(product)s came back %(condition)s, so it cannot go back on sale. "
                    "Only a sealed, in-date pack may be restocked.",
                    product=line.product_id.display_name,
                    condition=dict(CONDITIONS)[line.condition].lower()))
            if line.lot_id.expiration_date and line.lot_id.expiration_date <= now:
                raise ValidationError(_(
                    "Batch %(lot)s of %(product)s expired on %(date)s and cannot be "
                    "restocked.", lot=line.lot_id.name,
                    product=line.product_id.display_name,
                    date=fields.Date.to_date(line.lot_id.expiration_date)))

    @api.constrains('quantity')
    def _check_quantity(self):
        for line in self:
            if line.quantity <= 0:
                raise ValidationError(_("A return line needs a positive quantity."))

    @api.onchange('condition')
    def _onchange_condition(self):
        """Never leave 'restock' ticked when the condition no longer allows it."""
        if self.condition not in RESTOCKABLE_CONDITIONS:
            self.restock = False

    @api.onchange('product_id')
    def _onchange_product(self):
        if self.product_id:
            self.unit_price = self.product_id.list_price
