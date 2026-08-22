# -*- coding: utf-8 -*-
"""Controlled drugs register (BRD 5.7).

Regulators ask for a register: for every controlled substance, every movement in and
out, who handled it, for which patient, and a running balance that can be reconciled
against a physical count.

Records are append-only by design. Write and unlink are blocked even for
administrators - a register that can be edited after the fact is not a register. A
correcting entry (an adjustment) is the way to fix a mistake, exactly as in a
paper-based controlled drugs book.
"""

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

MOVEMENT_TYPES = [
    ('receipt', 'Received'),
    ('dispense', 'Dispensed'),
    ('return', 'Returned'),
    ('disposal', 'Disposed / Destroyed'),
    ('adjustment', 'Adjustment'),
    ('count', 'Physical Count'),
]


class PharmacyControlledLog(models.Model):
    _name = 'pharmacy.controlled.log'
    _description = 'Controlled Drugs Register Entry'

    _order = 'date desc, id desc'

    name = fields.Char('Entry', copy=False, readonly=True, default=lambda s: _('New'))
    date = fields.Datetime('Date', required=True, default=fields.Datetime.now, index=True)
    product_id = fields.Many2one(
        'product.product', string='Substance', required=True, index=True,
        domain="[('is_controlled', '=', True)]")
    controlled_schedule = fields.Selection(
        related='product_id.controlled_schedule', store=True, readonly=True)
    lot_id = fields.Many2one('stock.lot', string='Batch')
    movement_type = fields.Selection(
        MOVEMENT_TYPES, string='Movement', required=True, index=True)
    # Signed: positive in, negative out. Keeps the running balance a plain sum.
    quantity = fields.Float(
        'Quantity', required=True,
        help='Positive for stock coming in, negative for stock going out.')
    balance_after = fields.Float(
        'Balance', readonly=True,
        help='Running balance for this substance at the time of the entry.')

    pharmacist_id = fields.Many2one(
        'res.users', string='Handled By', required=True, default=lambda s: s.env.user)
    witness_id = fields.Many2one(
        'res.users', string='Witness',
        help='Second signatory, required by some jurisdictions for destruction.')
    patient_id = fields.Many2one('res.partner', string='Patient')
    prescription_id = fields.Many2one('pharmacy.prescription', string='Prescription')
    dispense_id = fields.Many2one('pharmacy.dispense', string='Dispensing')
    reason = fields.Text('Reason / Reference')

    company_id = fields.Many2one(
        'res.company', default=lambda self: self.env.company, required=True)

    @api.constrains('product_id')
    def _check_product_is_controlled(self):
        for entry in self:
            if not entry.product_id.is_controlled:
                raise ValidationError(_(
                    "%s is not flagged as a controlled drug, so it does not belong in "
                    "the controlled drugs register.", entry.product_id.display_name))

    @api.constrains('movement_type', 'quantity')
    def _check_direction(self):
        """Keep the sign consistent with the movement, or the balance becomes fiction."""
        for entry in self:
            outgoing = entry.movement_type in ('dispense', 'disposal')
            incoming = entry.movement_type in ('receipt', 'return')
            if outgoing and entry.quantity > 0:
                raise ValidationError(_(
                    "A %s entry removes stock and must have a negative quantity.",
                    dict(MOVEMENT_TYPES)[entry.movement_type]))
            if incoming and entry.quantity < 0:
                raise ValidationError(_(
                    "A %s entry adds stock and must have a positive quantity.",
                    dict(MOVEMENT_TYPES)[entry.movement_type]))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'pharmacy.controlled.log') or _('New')
        entries = super().create(vals_list)
        entries._recompute_balance()
        return entries

    def _recompute_balance(self):
        """Stamp the running balance per substance.

        Written with sudo deliberately: nobody has write access to the register (it is
        append-only), so the system must stamp its own derived field.
        """
        for entry in self.sudo():
            previous = self.sudo().search([
                ('product_id', '=', entry.product_id.id),
                ('company_id', '=', entry.company_id.id),
                ('id', '!=', entry.id),
                '|', ('date', '<', entry.date),
                     '&', ('date', '=', entry.date), ('id', '<', entry.id),
            ])
            entry.balance_after = sum(previous.mapped('quantity')) + entry.quantity

    def write(self, vals):
        """Append-only: the register is evidence, not a working document."""
        # The balance stamp is written by the system itself right after creation.
        if set(vals) - {'balance_after'}:
            raise UserError(_(
                "Controlled drugs register entries cannot be edited. Post a correcting "
                "Adjustment entry instead — the original entry stays on record."))
        return super().write(vals)

    def unlink(self):
        raise UserError(_(
            "Controlled drugs register entries cannot be deleted. Post a correcting "
            "Adjustment entry instead."))

    @api.constrains('movement_type', 'witness_id')
    def _check_disposal_witness(self):
        for entry in self:
            if entry.movement_type == 'disposal' and not entry.witness_id:
                raise ValidationError(_(
                    "Destruction of a controlled substance must be witnessed. Record "
                    "the second signatory in the Witness field."))


class StockLotPharmacy(models.Model):
    """Expiry management helpers on batches (BRD 5.8)."""
    _inherit = 'stock.lot'

    is_medicine_lot = fields.Boolean(
        related='product_id.is_medicine', store=True, readonly=True)
    is_expired = fields.Boolean(compute='_compute_expiry_state', search='_search_is_expired')
    days_to_expiry = fields.Integer(compute='_compute_expiry_state')

    @api.depends('expiration_date')
    def _compute_expiry_state(self):
        today = fields.Date.today()
        for lot in self:
            if lot.expiration_date:
                expiry = fields.Date.to_date(lot.expiration_date)
                lot.days_to_expiry = (expiry - today).days
                lot.is_expired = expiry < today
            else:
                lot.days_to_expiry = 0
                lot.is_expired = False

    def _search_is_expired(self, operator, value):
        """Make "expired" filterable, since the field is computed from today's date."""
        if operator not in ('=', '!=') or not isinstance(value, bool):
            raise UserError(_("Unsupported search on expired batches."))
        expired_wanted = (operator == '=') == value
        now = fields.Datetime.now()
        if expired_wanted:
            return [('expiration_date', '<', now), ('expiration_date', '!=', False)]
        return ['|', ('expiration_date', '=', False), ('expiration_date', '>=', now)]
