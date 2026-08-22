# -*- coding: utf-8 -*-
"""Tariff engine (SRS 11, 13).

A tariff turns a consumption figure into money. Three usage structures are supported:

  * flat    — one rate per unit
  * tiered  — MARGINAL blocks: each block is charged at its own rate, so 250 kWh over
              blocks 0-100@0.10 / 101-300@0.15 costs 100*0.10 + 150*0.15. This is how
              utilities bill worldwide, and it produces the per-block invoice breakdown.
  * base    — no usage rate; a fixed recurring service fee (e.g. an internet plan)

Everything else the SRS asks a tariff to express — service fees, penalties, discounts,
subsidies, minimum and maximum charges — is a CONFIGURED CHARGE LINE
(utility.tariff.charge), not code. A utility that wants "5% late penalty, 10 unit minimum,
pensioner subsidy" adds three rows; nobody edits Python.

The whole billing formula of SRS 13 therefore lives in one method, compute_charges():

    Current Charges = Usage + Fixed + Fees + Penalties - Discounts - Credits

Tax is a native account.tax so Odoo computes it on the invoice. Effective dates let a
seasonal rate supersede another without deleting history.
"""

from odoo import models, fields, api, _
from odoo.exceptions import ValidationError

UTILITY_TYPES = [
    ('water', 'Water'),
    ('electricity', 'Electricity'),
    ('gas', 'Gas'),
    ('internet', 'Internet'),
]

# Charge kinds that reduce the bill. Their computed amount is applied negatively.
CREDIT_KINDS = ('discount', 'subsidy')


class UtilityTariff(models.Model):
    _name = 'utility.tariff'
    _description = 'Utility Tariff'
    _inherit = ['mail.thread']
    _order = 'utility_type, date_start desc, id desc'

    name = fields.Char('Tariff Name', required=True, tracking=True)
    utility_type = fields.Selection(UTILITY_TYPES, string='Utility', required=True,
                                    tracking=True)
    structure = fields.Selection([
        ('flat', 'Flat Rate'),
        ('tiered', 'Tiered / Block'),
        ('base', 'Base Charge Only'),
    ], string='Structure', default='flat', required=True, tracking=True)

    currency_id = fields.Many2one(
        'res.currency', default=lambda s: s.env.company.currency_id, required=True)
    unit_name = fields.Char('Unit', default='unit',
                            help='Consumption unit shown on bills, e.g. kWh, m³.')
    flat_rate = fields.Monetary('Rate per Unit', currency_field='currency_id',
                                help='Used when the structure is Flat.')
    base_charge = fields.Monetary('Fixed Base Charge', currency_field='currency_id',
                                  help='Recurring service fee added regardless of usage.')
    block_ids = fields.One2many('utility.tariff.block', 'tariff_id', string='Blocks',
                                copy=True)
    charge_ids = fields.One2many('utility.tariff.charge', 'tariff_id', string='Charges',
                                 copy=True,
                                 help='Fees, penalties, discounts, subsidies and the '
                                      'minimum / maximum charge for this tariff.')

    tax_ids = fields.Many2many('account.tax', string='Taxes',
                               domain="[('type_tax_use', '=', 'sale')]")
    # The service product carries the income account + default taxes for the invoice
    # line. Auto-resolved per utility type; see _billing_product on the meter.
    product_id = fields.Many2one('product.product', string='Billing Product',
                                 help='Service product used for the invoice line '
                                      '(income account). Defaults per utility type.')

    # --- eligibility (SRS 11) ---
    customer_category_ids = fields.Many2many(
        'res.partner.category', string='Customer Categories',
        help='Restrict this tariff to customers carrying one of these tags — the SRS '
             '"customer category" axis (domestic, commercial, government...). Empty means '
             'it applies to anyone.')
    area = fields.Char('Geographic Area',
                       help='Restrict to service accounts in this area / zone. Empty '
                            'means everywhere.')

    date_start = fields.Date('Effective From', tracking=True)
    date_end = fields.Date('Effective To', tracking=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company)

    @api.constrains('structure', 'block_ids')
    def _check_tiered_has_blocks(self):
        for tariff in self:
            if tariff.structure == 'tiered' and not tariff.block_ids:
                raise ValidationError(_(
                    "Tiered tariff '%s' needs at least one pricing block.", tariff.name))

    @api.constrains('date_start', 'date_end')
    def _check_dates(self):
        for tariff in self:
            if tariff.date_start and tariff.date_end and tariff.date_end < tariff.date_start:
                raise ValidationError(_("A tariff cannot end before it starts."))

    @api.constrains('block_ids')
    def _check_block_bounds(self):
        """Blocks must climb, and only the last one may be open-ended.

        Without this, a tariff whose blocks are out of order, overlapping, or all
        open-ended bills silently wrong amounts — the arithmetic still "works", it is just
        not the tariff anyone approved.
        """
        for tariff in self.filtered(lambda t: t.structure == 'tiered'):
            blocks = tariff.block_ids.sorted('sequence')
            previous_upper = 0.0
            for index, block in enumerate(blocks):
                is_last = index == len(blocks) - 1
                if not block.upper_limit:
                    if not is_last:
                        raise ValidationError(_(
                            "Only the LAST block of '%(t)s' may be left open-ended. Block "
                            "%(s)s has no upper limit but is not last.",
                            t=tariff.name, s=block.sequence))
                    continue
                if block.upper_limit <= previous_upper:
                    raise ValidationError(_(
                        "Blocks of '%(t)s' must increase: block %(s)s ends at %(u)s, "
                        "which is not above the previous block's %(p)s.",
                        t=tariff.name, s=block.sequence, u=block.upper_limit,
                        p=previous_upper))
                previous_upper = block.upper_limit

    def _is_effective_on(self, date):
        """True if this tariff applies on the given date."""
        self.ensure_one()
        if self.date_start and date < self.date_start:
            return False
        if self.date_end and date > self.date_end:
            return False
        return True

    def _ordered_bounds(self):
        """[(lower, upper), ...] for the tiered blocks, in charging order.

        The upper bound of the last block is infinity when it is left empty. Everywhere
        else an empty value is rejected by _check_block_bounds — which is the fix for the
        old `upper_limit or inf` expression, where a block ending at 0 silently became
        "and above" and swallowed the entire consumption at its own rate.
        """
        self.ensure_one()
        blocks = self.block_ids.sorted('sequence')
        bounds = []
        lower = 0.0
        for index, block in enumerate(blocks):
            is_last = index == len(blocks) - 1
            upper = block.upper_limit if block.upper_limit else (
                float('inf') if is_last else lower)
            bounds.append((lower, upper, block))
            lower = upper
        return bounds

    # ------------------------------------------------------------------
    # The billing formula (SRS 13)
    # ------------------------------------------------------------------
    def compute_charges(self, consumption, apply_penalties=False, overdue_amount=0.0):
        """Return every line this tariff produces for a consumption figure.

        Each line is {'kind', 'label', 'quantity', 'price_unit', 'subtotal'}, in the order
        they belong on a bill: usage blocks, the fixed base charge, fees, the minimum or
        maximum adjustment, then discounts and subsidies, then penalties. The caller turns
        them into invoice lines; nothing here touches the database.

        `apply_penalties` is off by default: a penalty is a decision about an ACCOUNT's
        payment history, not a property of the reading, so only a billing run that has
        checked the balance may switch it on.
        """
        self.ensure_one()
        consumption = max(consumption or 0.0, 0.0)
        lines = self._usage_lines(consumption)

        if self.base_charge:
            lines.append({
                'kind': 'base',
                'label': _('%s — base charge', self.name),
                'quantity': 1.0,
                'price_unit': self.base_charge,
                'subtotal': self.base_charge,
            })

        usage_total = sum(line['subtotal'] for line in lines)
        lines += self._charge_lines(consumption, usage_total, apply_penalties,
                                    overdue_amount)
        return lines

    def _usage_lines(self, consumption):
        """The consumption part of the bill: flat, marginal blocks, or nothing."""
        self.ensure_one()
        lines = []
        if self.structure == 'base' or not consumption:
            return lines

        if self.structure == 'flat':
            lines.append({
                'kind': 'usage',
                'label': _('%(name)s — %(qty)s %(unit)s',
                           name=self.name, qty=consumption, unit=self.unit_name),
                'quantity': consumption,
                'price_unit': self.flat_rate,
                'subtotal': consumption * self.flat_rate,
            })
            return lines

        # tiered — charge only the slice of consumption that falls inside each block
        remaining = consumption
        for lower, upper, block in self._ordered_bounds():
            if remaining <= 0:
                break
            span = upper - lower
            quantity = min(remaining, span)
            if quantity <= 0:
                continue
            lines.append({
                'kind': 'usage',
                'label': _('%(name)s — Block %(seq)s (%(lo)s-%(hi)s %(unit)s)',
                           name=self.name, seq=block.sequence, lo=int(lower),
                           hi=('∞' if upper == float('inf') else int(upper)),
                           unit=self.unit_name),
                'quantity': quantity,
                'price_unit': block.rate,
                'subtotal': quantity * block.rate,
            })
            remaining -= quantity
        return lines

    def _charge_lines(self, consumption, usage_total, apply_penalties, overdue_amount):
        """Fees, min/max, discounts, subsidies and penalties, in that order."""
        self.ensure_one()
        lines = []
        running_total = usage_total

        for charge in self.charge_ids.filtered('active').sorted('sequence'):
            if charge.kind == 'penalty' and not apply_penalties:
                continue
            if not charge._applies_to(consumption, running_total):
                continue
            amount = charge._compute_amount(consumption, running_total, overdue_amount)
            if not amount:
                continue
            if charge.kind in CREDIT_KINDS:
                amount = -abs(amount)
            lines.append({
                'kind': charge.kind,
                'label': charge.name,
                'quantity': 1.0,
                'price_unit': amount,
                'subtotal': amount,
                'charge_id': charge.id,
            })
            running_total += amount
        return lines


class UtilityTariffBlock(models.Model):
    _name = 'utility.tariff.block'
    _description = 'Utility Tariff Block'
    _order = 'sequence, id'

    tariff_id = fields.Many2one('utility.tariff', required=True, ondelete='cascade')
    sequence = fields.Integer(default=1)
    lower_limit = fields.Float('From', compute='_compute_lower_limit',
                               help='Where this block starts — the previous block\'s '
                                    'upper limit. Shown so a tariff can be read at a '
                                    'glance instead of inferred.')
    upper_limit = fields.Float(
        'Up To', help='Upper bound of this block in consumption units. Leave empty on '
                      'the LAST block to mean "and above"; anywhere else an empty or '
                      'zero value is rejected.')
    rate = fields.Monetary('Rate per Unit', currency_field='currency_id')
    currency_id = fields.Many2one(related='tariff_id.currency_id', readonly=True)

    @api.depends('tariff_id.block_ids.upper_limit', 'tariff_id.block_ids.sequence',
                 'sequence')
    def _compute_lower_limit(self):
        for block in self:
            lower = 0.0
            for other in block.tariff_id.block_ids.sorted('sequence'):
                if other == block or (other._origin and other._origin == block._origin):
                    break
                lower = other.upper_limit or lower
            block.lower_limit = lower

    @api.constrains('upper_limit')
    def _check_upper(self):
        for block in self:
            if block.upper_limit < 0:
                raise ValidationError(_("Block upper limit cannot be negative."))


class UtilityTariffCharge(models.Model):
    _name = 'utility.tariff.charge'
    _description = 'Utility Tariff Charge'
    _order = 'sequence, id'

    tariff_id = fields.Many2one('utility.tariff', required=True, ondelete='cascade',
                                index=True)
    sequence = fields.Integer(default=10)
    name = fields.Char('Description', required=True,
                       help='Printed on the bill exactly as written here.')
    kind = fields.Selection([
        ('fee', 'Service Fee'),
        ('penalty', 'Penalty / Late Fee'),
        ('discount', 'Discount'),
        ('subsidy', 'Subsidy'),
        ('minimum', 'Minimum Charge'),
        ('maximum', 'Maximum Charge'),
    ], required=True, default='fee')
    computation = fields.Selection([
        ('fixed', 'Fixed Amount'),
        ('percent', 'Percentage of Charges'),
        ('per_unit', 'Per Consumption Unit'),
    ], required=True, default='fixed')
    amount = fields.Float('Amount / Percentage', required=True,
                          help='A fixed sum, a percentage of the charges so far, or a '
                               'rate per consumption unit, depending on the computation.')
    apply_when = fields.Selection([
        ('always', 'Always'),
        ('usage_above', 'Consumption Above Threshold'),
        ('usage_below', 'Consumption Below Threshold'),
    ], default='always', required=True)
    threshold = fields.Float('Threshold')
    active = fields.Boolean(default=True)
    currency_id = fields.Many2one(related='tariff_id.currency_id', readonly=True)

    @api.constrains('kind', 'computation')
    def _check_minimum_maximum(self):
        for charge in self:
            if charge.kind in ('minimum', 'maximum') and charge.computation != 'fixed':
                raise ValidationError(_(
                    "A %s charge is a monetary floor or ceiling, so it must be a fixed "
                    "amount.", charge.kind))

    def _applies_to(self, consumption, running_total):
        self.ensure_one()
        if self.apply_when == 'usage_above':
            return consumption > self.threshold
        if self.apply_when == 'usage_below':
            return consumption < self.threshold
        return True

    def _compute_amount(self, consumption, running_total, overdue_amount=0.0):
        """The money this charge adds (positive) — the caller flips the sign for credits.

        Minimum and maximum are ADJUSTMENTS, not charges: they top the bill up to, or trim
        it down to, the configured figure, so applying one twice cannot compound.
        """
        self.ensure_one()
        if self.kind == 'minimum':
            shortfall = self.amount - running_total
            return shortfall if shortfall > 0 else 0.0
        if self.kind == 'maximum':
            excess = running_total - self.amount
            return -excess if excess > 0 else 0.0
        if self.computation == 'fixed':
            return self.amount
        if self.computation == 'per_unit':
            return self.amount * consumption
        # percent — a late penalty is charged on what is overdue, everything else on the
        # charges accumulated so far.
        basis = overdue_amount if self.kind == 'penalty' and overdue_amount else running_total
        return basis * (self.amount / 100.0)
