# -*- coding: utf-8 -*-
"""Dispensing (BRD 5.4 partial dispensing, 5.2 batch tracking).

A dispensing is a single hand-over event. Confirming one does real work:

  1. checks the prescription is verified, unexpired and still has quantity outstanding
  2. blocks expired batches and warns on near-expiry ones
  3. creates and confirms a sale.order, which produces the delivery and the stock move
  4. forces the chosen batch onto the move where the pharmacist picked one, otherwise
     leaves Odoo's FEFO strategy to choose the earliest-expiring lot
  5. writes controlled substances to the register

Point 3 is the part the previous module lacked: it flipped a status field and never
moved any stock, so the pharmacy's inventory silently drifted from reality.
"""

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

# Batches expiring within this window are flagged to the pharmacist at dispensing.
NEAR_EXPIRY_WARN_DAYS = 30


class PharmacyDispense(models.Model):
    _name = 'pharmacy.dispense'
    _description = 'Medicine Dispensing'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'dispense_date desc, id desc'

    name = fields.Char('Reference', copy=False, readonly=True, index=True,
                       default=lambda s: _('New'))
    prescription_id = fields.Many2one(
        'pharmacy.prescription', string='Prescription', required=True,
        ondelete='restrict', index=True, tracking=True)
    patient_id = fields.Many2one(
        related='prescription_id.patient_id', store=True, readonly=True)
    patient_allergies = fields.Text(
        related='prescription_id.patient_id.allergies', readonly=True)
    dispense_date = fields.Datetime(
        'Dispensed On', default=fields.Datetime.now, required=True, tracking=True)
    pharmacist_id = fields.Many2one(
        'res.users', string='Dispensed By', default=lambda s: s.env.user,
        required=True, tracking=True)
    line_ids = fields.One2many('pharmacy.dispense.line', 'dispense_id', string='Lines')

    state = fields.Selection([
        ('draft', 'Draft'),
        ('done', 'Dispensed'),
        ('cancelled', 'Cancelled'),
    ], default='draft', required=True, tracking=True, copy=False, index=True)

    sale_order_id = fields.Many2one(
        'sale.order', string='Sale Order', readonly=True, copy=False,
        help='Sales document this dispensing created; it carries the stock move and '
             'the invoice.')
    picking_ids = fields.One2many(
        related='sale_order_id.picking_ids', string='Deliveries', readonly=True)
    # Computed from THIS dispensing's own lines, not from the prescription. Two
    # reasons: it is more accurate (a prescription may list a controlled drug that is
    # not part of this particular hand-over, and the pharmacist requirement should
    # follow what actually leaves the shelf), and it keeps the dependency inside this
    # model - a stored related/depends pointing at a computed field on another model
    # cannot be resolved during registry setup.
    has_controlled = fields.Boolean(
        string='Contains Controlled Drug',
        compute='_compute_has_controlled', store=True, readonly=True)
    # Set when a refill cycle starts: keeps the record as history without counting
    # against the new cycle's outstanding quantities.
    is_previous_cycle = fields.Boolean(
        'Previous Refill Cycle', default=False, readonly=True, copy=False)
    notes = fields.Text('Notes')

    company_id = fields.Many2one(
        'res.company', default=lambda self: self.env.company, required=True)

    @api.depends('line_ids.product_id.is_controlled')
    def _compute_has_controlled(self):
        for dispense in self:
            dispense.has_controlled = any(
                dispense.line_ids.mapped('product_id.is_controlled'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'pharmacy.dispense') or _('New')
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Confirmation
    # ------------------------------------------------------------------
    def action_confirm(self):
        for dispense in self:
            if dispense.state != 'draft':
                raise UserError(_("%s has already been processed.", dispense.name))
            if not dispense.line_ids:
                raise UserError(_("Nothing to dispense on %s.", dispense.name))

            dispense.prescription_id._assert_dispensable()
            dispense._check_clinical_reviewed()
            dispense._check_pharmacist_required()
            dispense._check_quantities()
            dispense._check_batches()

            order = dispense._create_sale_order()
            dispense.sale_order_id = order
            dispense.state = 'done'
            dispense._apply_chosen_lots()
            dispense._log_controlled_movements()
            dispense.prescription_id._refresh_dispensing_state()
            dispense.message_post(body=_(
                "Dispensed against %(rx)s; sale order %(so)s created.",
                rx=dispense.prescription_id.name, so=order.name))
        return True

    def _check_clinical_reviewed(self):
        """Do not hand over medicine carrying an unreviewed serious warning.

        Verification already forces this, but a script can be verified and then have a
        medicine added, or the patient's allergies updated, between verification and
        the counter. Re-checking here means the last word belongs to the moment the
        medicine actually changes hands.
        """
        self.ensure_one()
        rx = self.prescription_id
        if rx.clinical_severity in ('high', 'critical') and not rx.clinical_ack_by_id:
            raise UserError(_(
                "%(name)s carries %(severity)s clinical warnings that no pharmacist has "
                "reviewed:\n\n%(warnings)s",
                name=rx.name, severity=rx.clinical_severity,
                warnings=rx.clinical_warning or ''))

    def _check_pharmacist_required(self):
        """Controlled drugs may only be handed over by a pharmacist (BRD 5.7)."""
        self.ensure_one()
        if self.has_controlled and not self.env.user.has_group(
                'sahal_pharmacy.group_pharmacy_pharmacist'):
            raise UserError(_(
                "%s contains a controlled drug, which only a pharmacist may dispense.",
                self.prescription_id.name))

    def _check_quantities(self):
        """Never dispense more than the prescriber authorised."""
        self.ensure_one()
        for line in self.line_ids:
            rx_line = line.prescription_line_id
            if not rx_line:
                continue
            if line.quantity > rx_line.qty_remaining + 1e-6:
                raise UserError(_(
                    "%(product)s: trying to dispense %(qty)s but only %(left)s remain "
                    "on the prescription.",
                    product=line.product_id.display_name,
                    qty=line.quantity, left=rx_line.qty_remaining))

    def _check_batches(self):
        """Refuse expired stock outright; surface near-expiry batches in the chatter."""
        self.ensure_one()
        today = fields.Date.today()
        warnings = []
        for line in self.line_ids.filtered('lot_id'):
            expiry = line.lot_id.expiration_date
            if not expiry:
                continue
            expiry_date = fields.Date.to_date(expiry)
            if expiry_date < today:
                raise UserError(_(
                    "Batch %(lot)s of %(product)s expired on %(date)s and cannot be "
                    "dispensed.",
                    lot=line.lot_id.name, product=line.product_id.display_name,
                    date=expiry_date))
            if (expiry_date - today).days <= NEAR_EXPIRY_WARN_DAYS:
                warnings.append(_(
                    "%(product)s batch %(lot)s expires on %(date)s.",
                    product=line.product_id.display_name, lot=line.lot_id.name,
                    date=expiry_date))
        if warnings:
            self.message_post(body=_("Near-expiry batches dispensed: %s",
                                     " ".join(warnings)))

    def _create_sale_order(self):
        """Turn the dispensing into a confirmed sale order.

        Confirming is what generates the delivery and therefore the stock move, so
        inventory reflects the hand-over. Invoicing stays with the normal sales flow
        (or POS) rather than being forced here.
        """
        self.ensure_one()
        order = self.env['sale.order'].create({
            'partner_id': self.patient_id.id,
            'company_id': self.company_id.id,
            'origin': self.prescription_id.name,
            'pharmacy_prescription_id': self.prescription_id.id,
            'order_line': [(0, 0, {
                'product_id': line.product_id.id,
                'product_uom_qty': line.quantity,
                'name': line._sale_line_description(),
            }) for line in self.line_ids],
        })
        # pharmacy_dispensing exempts this order from the point-of-sale prescription
        # guard: the pharmacist already verified the script, and this method has
        # just run the quantity, expiry and controlled-drug checks itself.
        order.with_context(pharmacy_dispensing=True).action_confirm()
        return order

    def _apply_chosen_lots(self):
        """Force the pharmacist's batch choice onto the delivery move lines.

        Where no batch was chosen we deliberately leave the move alone so Odoo's FEFO
        removal strategy picks the earliest-expiring lot - which is the behaviour a
        pharmacy wants by default.
        """
        self.ensure_one()
        moves = self.sale_order_id.picking_ids.move_ids
        for line in self.line_ids.filtered('lot_id'):
            move = moves.filtered(lambda m: m.product_id == line.product_id)[:1]
            if not move:
                continue
            move_lines = move.move_line_ids
            if move_lines:
                move_lines[0].lot_id = line.lot_id
            else:
                # Nothing could be reserved (e.g. no stock on hand). Note the promised
                # batch on the picking rather than failing the hand-over, so the
                # storekeeper still knows what was committed.
                move.picking_id.message_post(body=_(
                    "Batch %(lot)s was selected for %(product)s but no stock could be "
                    "reserved.",
                    lot=line.lot_id.name, product=line.product_id.display_name))

    def _log_controlled_movements(self):
        """Write every controlled-substance hand-over to the register (BRD 5.7)."""
        self.ensure_one()
        Register = self.env['pharmacy.controlled.log']
        for line in self.line_ids.filtered(lambda l: l.product_id.is_controlled):
            Register.sudo().create({
                'product_id': line.product_id.id,
                'lot_id': line.lot_id.id or False,
                'movement_type': 'dispense',
                'quantity': -abs(line.quantity),
                'date': self.dispense_date,
                'pharmacist_id': self.pharmacist_id.id,
                'patient_id': self.patient_id.id,
                'prescription_id': self.prescription_id.id,
                'dispense_id': self.id,
                'company_id': self.company_id.id,
            })

    def action_cancel(self):
        for dispense in self:
            if dispense.state == 'done':
                raise UserError(_(
                    "%s is already dispensed. Cancel the sale order %s and return the "
                    "stock instead — a dispensing record is part of the audit trail "
                    "and is never deleted.",
                    dispense.name, dispense.sale_order_id.name or ''))
            dispense.state = 'cancelled'

    def action_view_sale_order(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'res_id': self.sale_order_id.id,
            'view_mode': 'form',
        }


class PharmacyDispenseLine(models.Model):
    _name = 'pharmacy.dispense.line'
    _description = 'Dispensing Line'


    dispense_id = fields.Many2one(
        'pharmacy.dispense', required=True, ondelete='cascade', index=True)
    prescription_line_id = fields.Many2one(
        'pharmacy.prescription.line', string='Prescription Line', ondelete='restrict')
    product_id = fields.Many2one(
        'product.product', string='Medicine', required=True,
        domain="[('is_medicine', '=', True)]")
    lot_id = fields.Many2one(
        'stock.lot', string='Batch',
        domain="[('product_id', '=', product_id)]",
        help='Leave empty to let Odoo pick the earliest-expiring batch (FEFO).')
    expiration_date = fields.Datetime(
        related='lot_id.expiration_date', readonly=True, string='Batch Expiry')
    quantity = fields.Float('Quantity', default=1.0, required=True)
    uom_id = fields.Many2one(related='product_id.uom_id', readonly=True)
    qty_available = fields.Float(
        related='product_id.qty_available', readonly=True, string='On Hand')

    @api.constrains('quantity')
    def _check_quantity_positive(self):
        for line in self:
            if line.quantity <= 0:
                raise ValidationError(_("Dispensed quantity must be greater than zero."))

    def _sale_line_description(self):
        """Put the dosage instructions on the sales line, so they reach the receipt."""
        self.ensure_one()
        rx_line = self.prescription_line_id
        parts = [self.product_id.display_name]
        if rx_line:
            detail = " ".join(filter(None, [
                rx_line.dosage, rx_line.frequency, rx_line.instructions]))
            if detail:
                parts.append(detail)
        return " — ".join(parts)
