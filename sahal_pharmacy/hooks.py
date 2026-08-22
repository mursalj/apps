# -*- coding: utf-8 -*-
"""Post-install wiring.

Installing an application should make it usable, and an Odoo app is invisible until
the user holds one of its groups. The Pharmacy menu is gated on the cashier role, so
without this step a platform operator installs the module and sees nothing at all.

Only existing system administrators are granted the role, and only the manager
role, which implies the rest.
"""

import logging

_logger = logging.getLogger(__name__)


def post_init_hook(env):
    manager = env.ref('sahal_pharmacy.group_pharmacy_manager', raise_if_not_found=False)
    system = env.ref('base.group_system', raise_if_not_found=False)
    if not manager or not system:
        return

    # Direct membership only: has_group() would follow implied groups and could match
    # unrelated third-party groups that transitively imply base.group_system.
    admins = env['res.users'].sudo().search([
        ('group_ids', 'in', system.id),
        ('id', 'not in', manager.all_user_ids.ids),
        ('active', '=', True),
    ])
    if admins:
        admins.write({'group_ids': [(4, manager.id)]})
        _logger.info("Pharmacy Manager role granted to %d administrator(s): %s",
                     len(admins), ', '.join(admins.mapped('login')))
