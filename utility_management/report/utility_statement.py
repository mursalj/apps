# -*- coding: utf-8 -*-
"""Customer account statement (SRS 36).

The SRS asks for a running ledger per customer — date, transaction, debit, credit,
balance — downloadable as a PDF. Odoo's partner ledger is close, but it is per PARTNER and
per accounting account: a customer with an electricity account and a water account sees
one merged column of numbers and cannot tell which connection owes what.

This builds the ledger per SERVICE ACCOUNT, from the invoices stamped with it and the
payments reconciled against those invoices.
"""

from odoo import models, api, fields, _


class UtilityStatementReport(models.AbstractModel):
    _name = 'report.utility_management.report_utility_statement'
    _description = 'Utility Account Statement'

    @api.model
    def _get_report_values(self, docids, data=None):
        accounts = self.env['utility.service.account'].browse(docids)
        return {
            'doc_ids': docids,
            'doc_model': 'utility.service.account',
            'docs': accounts,
            'statement_lines': {a.id: self._statement_lines(a) for a in accounts},
            'opening': {a.id: 0.0 for a in accounts},
        }

    def _statement_lines(self, account):
        """Chronological debit/credit rows with a running balance.

        Settlements are read from the PARTIAL RECONCILIATIONS on each invoice's receivable
        line, not from account.move.matched_payment_ids: that field only recognises a
        payment made through the payment model, so a credit note, a manual journal entry
        or a write-off would silently vanish from the customer's ledger while still
        changing what they owe. A partial reconciliation is the ledger fact itself, and it
        carries the amount that actually landed on this invoice.
        """
        rows = []
        invoices = self.env['account.move'].search([
            ('utility_account_id', '=', account.id),
            ('move_type', 'in', ('out_invoice', 'out_refund')),
            ('state', '=', 'posted'),
        ], order='invoice_date asc, id asc')

        for invoice in invoices:
            sign = 1 if invoice.move_type == 'out_invoice' else -1
            amount = invoice.amount_total_signed if hasattr(invoice, 'amount_total_signed') \
                else invoice.amount_total * sign
            rows.append({
                'date': invoice.invoice_date,
                'label': _('Invoice %s', invoice.name),
                'reference': invoice.name,
                'debit': amount if amount > 0 else 0.0,
                'credit': -amount if amount < 0 else 0.0,
                'period_start': invoice.utility_period_start,
                'period_end': invoice.utility_period_end,
            })
            for settlement in self._settlements(invoice):
                rows.append(settlement)

        rows.sort(key=lambda r: (r['date'] or fields.Date.today(), r['reference'] or ''))
        balance = 0.0
        for row in rows:
            balance += row['debit'] - row['credit']
            row['balance'] = balance
        return rows

    def _settlements(self, invoice):
        """Everything that reduced this invoice: payments, credit notes, write-offs."""
        receivable = invoice.line_ids.filtered(
            lambda l: l.account_id.account_type in ('asset_receivable', 'liability_payable'))
        rows = []
        for line in receivable:
            # A customer invoice sits on the debit side, so its settlements arrive as
            # matched CREDITS; a credit note is the mirror image.
            for partial in line.matched_credit_ids:
                counterpart = partial.credit_move_id
                rows.append(self._settlement_row(partial, counterpart, credit=True))
            for partial in line.matched_debit_ids:
                counterpart = partial.debit_move_id
                rows.append(self._settlement_row(partial, counterpart, credit=False))
        return rows

    def _settlement_row(self, partial, counterpart, credit=True):
        move = counterpart.move_id
        # origin_payment_id in Odoo 19; a plain journal entry is a manual settlement.
        is_payment = bool(move.origin_payment_id) if 'origin_payment_id' in move._fields \
            else move.move_type == 'entry'
        label = _('Payment %s', move.name) if is_payment or move.move_type == 'entry' \
            else _('Credit note %s', move.name)
        return {
            'date': counterpart.date or move.date,
            'label': label,
            'reference': move.name or '',
            'debit': 0.0 if credit else partial.amount,
            'credit': partial.amount if credit else 0.0,
            'period_start': False,
            'period_end': False,
        }
