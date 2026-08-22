# -*- coding: utf-8 -*-
"""Returns to supplier (PRD 64).

    Expired / damaged stock -> Return request -> Approval -> Return shipment
    -> Credit note -> Inventory adjusted

Odoo can already return a receipt and raise a vendor credit note, but only as two
unlinked operations started from different screens, with nothing recording WHY the
stock is going back, which batch it was, or who agreed to it. For a pharmacy that
record is the point: expired stock leaving the building is a regulated event, and
"which supplier keeps sending us short-dated stock" is a question the pharmacy has to
be able to answer (PRD 82).

So this model is a thin case file over Odoo's own machinery: it creates a real picking
and a real vendor credit note, and holds the reason, the batch and the approval.
"""

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

RETURN_REASONS = [
    ('expired', 'Expired'),
    ('near_expiry', 'Near Expiry'),
    ('damaged', 'Damaged'),
    ('recall', 'Recall'),
    ('wrong_item', 'Wrong Item Delivered'),
    ('quality', 'Quality Failure'),
    ('overstock', 'Overstock / Overordered'),
    ('other', 'Other'),
]


class PharmacySupplierReturn(models.Model):
    _name = 'pharmacy.supplier.return'
    _description = 'Return to Supplier'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'return_date desc, id desc'

    name = fields.Char('Reference', copy=False, readonly=True, index=True,
                       default=lambda s: _('New'))
    partner_id = fields.Many2one(
        'res.partner', string='Supplier', required=True, tracking=True,
        domain="[('is_pharma_supplier', '=', True)]",
        help='Who the stock goes back to.')
    return_date = fields.Date('Return Date', required=True, tracking=True,
                              default=fields.Date.context_today)
    reason = fields.Selection(RETURN_REASONS, string='Reason', required=True,
                              tracking=True)
    reason_note = fields.Text('Details')
    origin_picking_id = fields.Many2one(
        'stock.picking', string='Original Receipt',
        domain="[('partner_id', '=', partner_id), ('picking_type_code', '=', 'incoming')]",
        help='The delivery this stock arrived on, when it is known.')

    line_ids = fields.One2many('pharmacy.supplier.return.line', 'return_id',
                               string='Lines')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('approved', 'Approved'),
        ('returned', 'Shipped Back'),
        ('credited', 'Credited'),
        ('cancel', 'Cancelled'),
    ], default='draft', required=True, tracking=True)

    picking_id = fields.Many2one('stock.picking', string='Return Shipment',
                                 readonly=True, copy=False)
    credit_note_id = fields.Many2one('account.move', string='Credit Note',
                                     readonly=True, copy=False)
    approved_by_id = fields.Many2one('res.users', string='Approved By', readonly=True,
                                     copy=False)
    approved_on = fields.Datetime('Approved On', readonly=True, copy=False)

    total_quantity = fields.Float('Total Quantity', compute='_compute_totals',
                                  store=True)
    total_value = fields.Monetary('Total Value', compute='_compute_totals', store=True,
                                  currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id',
                                  readonly=True)
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company,
                                 required=True)

    @api.depends('line_ids.quantity', 'line_ids.unit_cost')
    def _compute_totals(self):
        for record in self:
            record.total_quantity = sum(record.line_ids.mapped('quantity'))
            record.total_value = sum(
                line.quantity * line.unit_cost for line in record.line_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'pharmacy.supplier.return') or _('New')
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def action_approve(self):
        """Agree that this stock leaves. A pharmacist or manager decision."""
        if not self.env.user.has_group('sahal_pharmacy.group_pharmacy_pharmacist'):
            raise UserError(_(
                "Returning stock to a supplier needs a pharmacist or manager: it moves "
                "regulated goods out of the pharmacy's custody."))
        for record in self:
            if record.state != 'draft':
                raise UserError(_("Only a draft return can be approved."))
            if not record.line_ids:
                raise UserError(_("Add at least one line before approving."))
            record.write({
                'state': 'approved',
                'approved_by_id': self.env.user.id,
                'approved_on': fields.Datetime.now(),
            })

    def action_create_shipment(self):
        """Build the outgoing picking that physically returns the stock."""
        self.ensure_one()
        if self.state != 'approved':
            raise UserError(_("Approve the return before shipping it back."))
        if self.picking_id:
            raise UserError(_("This return already has shipment %s.",
                              self.picking_id.name))

        picking_type = self.env['stock.picking.type'].search([
            ('code', '=', 'outgoing'), ('company_id', '=', self.company_id.id),
        ], limit=1)
        if not picking_type:
            raise UserError(_("No outgoing operation type is configured for %s.",
                              self.company_id.name))
        supplier_location = self.env.ref('stock.stock_location_suppliers')

        moves = []
        for line in self.line_ids:
            moves.append((0, 0, {
                # Odoo 19 dropped stock.move.name; the line's own wording lives in
                # description_picking, which is what prints on the transfer.
                'description_picking': _('Return: %s', line.product_id.display_name),
                'product_id': line.product_id.id,
                'product_uom_qty': line.quantity,
                'product_uom': line.product_id.uom_id.id,
                'location_id': picking_type.default_location_src_id.id,
                'location_dest_id': supplier_location.id,
            }))
        picking = self.env['stock.picking'].create({
            'partner_id': self.partner_id.id,
            'picking_type_id': picking_type.id,
            'location_id': picking_type.default_location_src_id.id,
            'location_dest_id': supplier_location.id,
            'origin': self.name,
            'company_id': self.company_id.id,
            'move_ids': moves,
        })
        # Carry the batch across: a return of expired stock is meaningless if it does
        # not say WHICH batch went back.
        for move, line in zip(picking.move_ids, self.line_ids):
            if line.lot_id:
                move.move_line_ids = [(0, 0, {
                    'product_id': line.product_id.id,
                    'lot_id': line.lot_id.id,
                    'quantity': line.quantity,
                    'location_id': move.location_id.id,
                    'location_dest_id': move.location_dest_id.id,
                })]
        picking.action_confirm()
        self.write({'picking_id': picking.id, 'state': 'returned'})
        return self.action_view_picking()

    def action_create_credit_note(self):
        """Raise the vendor credit note for what went back."""
        self.ensure_one()
        # Duplicate check FIRST: once a credit note exists the state has moved on, and
        # answering "ship it before claiming credit" to someone who already claimed it
        # sends them to look at the wrong thing.
        if self.credit_note_id:
            raise UserError(_("Credit note %s already exists for this return.",
                              self.credit_note_id.name or self.credit_note_id.id))
        if self.state not in ('returned', 'approved'):
            raise UserError(_("Ship the return before claiming a credit for it."))
        lines = []
        for line in self.line_ids:
            lines.append((0, 0, {
                'product_id': line.product_id.id,
                'name': _('%(product)s — returned (%(reason)s)',
                          product=line.product_id.display_name,
                          reason=dict(RETURN_REASONS)[self.reason]),
                'quantity': line.quantity,
                'price_unit': line.unit_cost,
            }))
        move = self.env['account.move'].with_company(self.company_id).create({
            'move_type': 'in_refund',
            'partner_id': self.partner_id.id,
            'invoice_date': self.return_date,
            'invoice_origin': self.name,
            'company_id': self.company_id.id,
            'invoice_line_ids': lines,
        })
        self.write({'credit_note_id': move.id, 'state': 'credited'})
        return self.action_view_credit_note()

    def action_cancel(self):
        for record in self:
            if record.credit_note_id and record.credit_note_id.state == 'posted':
                raise UserError(_(
                    "Credit note %s is posted. Reverse it before cancelling this "
                    "return.", record.credit_note_id.name))
            record.state = 'cancel'

    def action_reset_draft(self):
        for record in self:
            if record.picking_id and record.picking_id.state == 'done':
                raise UserError(_(
                    "The stock has already left on %s. Reverse that transfer first.",
                    record.picking_id.name))
            record.write({'state': 'draft', 'approved_by_id': False,
                          'approved_on': False})

    def action_view_picking(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'view_mode': 'form',
            'res_id': self.picking_id.id,
        }

    def action_view_credit_note(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.credit_note_id.id,
        }


class PharmacySupplierReturnLine(models.Model):
    _name = 'pharmacy.supplier.return.line'
    _description = 'Return to Supplier Line'


    return_id = fields.Many2one('pharmacy.supplier.return', required=True,
                                ondelete='cascade', index=True)
    product_id = fields.Many2one('product.product', string='Product', required=True,
                                 domain="[('is_medicine', '=', True)]")
    lot_id = fields.Many2one('stock.lot', string='Batch',
                             domain="[('product_id', '=', product_id)]")
    expiration_date = fields.Datetime(related='lot_id.expiration_date', readonly=True)
    quantity = fields.Float('Quantity', required=True, default=1.0)
    unit_cost = fields.Float('Unit Cost')
    company_id = fields.Many2one(related='return_id.company_id', store=True,
                                 readonly=True)

    @api.onchange('product_id')
    def _onchange_product(self):
        if self.product_id:
            self.unit_cost = self.product_id.standard_price

    @api.constrains('quantity')
    def _check_quantity(self):
        for line in self:
            if line.quantity <= 0:
                raise ValidationError(_("A return line must have a positive quantity."))
