# -*- coding: utf-8 -*-
"""Per-company reference numbering.

Account numbers, reading references and billing-run references all come from an
ir.sequence. The module ships those sequences with company_id = False, which makes them

Nothing leaks (the records themselves are isolated), but a customer-facing account number
with gaps in it invites exactly the question a utility cannot answer.

This helper resolves the sequence for the CURRENT company, creating a company-scoped copy
from the global one the first time that company needs a number. Odoo's next_by_code
already prefers a company-scoped sequence, so once the copy exists everything else works
unchanged.
"""

from odoo import models, api


class UtilitySequenceMixin(models.AbstractModel):
    _name = 'utility.sequence.mixin'
    _description = 'Utility Reference Numbering'

    @api.model
    def _next_reference(self, code):
        """Next number from `code`, scoped to the current company."""
        Sequence = self.env['ir.sequence'].sudo()
        company = self.env.company
        scoped = Sequence.search(
            [('code', '=', code), ('company_id', '=', company.id)], limit=1)
        if not scoped:
            template = Sequence.search(
                [('code', '=', code), ('company_id', '=', False)], limit=1)
            if not template:
                return False
            scoped = template.copy({
                'name': '%s (%s)' % (template.name, company.name),
                'company_id': company.id,
                'number_next': 1,
            })
        return scoped.next_by_id()
