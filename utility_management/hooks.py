# -*- coding: utf-8 -*-
"""Post-install wiring.

An Odoo app is invisible until the user holds one of its groups, and the Utilities menu
is gated on the technician role. So without this an operator installs the module and
sees nothing. Grant existing system administrators the Utility Manager role (which
MODULE_GROUPS_MAPPING when the module is allocated.
"""

import logging

_logger = logging.getLogger(__name__)


def post_init_hook(env):
    manager = env.ref('utility_management.group_utility_manager', raise_if_not_found=False)
    system = env.ref('base.group_system', raise_if_not_found=False)
    if not manager or not system:
        return
    admins = env['res.users'].sudo().search([
        ('group_ids', 'in', system.id),
        ('id', 'not in', manager.all_user_ids.ids),
        ('active', '=', True),
    ])
    if admins:
        admins.write({'group_ids': [(4, manager.id)]})
        _logger.info("Utility Manager role granted to %d administrator(s): %s",
                     len(admins), ', '.join(admins.mapped('login')))
