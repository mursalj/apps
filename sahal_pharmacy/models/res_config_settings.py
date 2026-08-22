# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # A pos_-prefixed field related to pos_config_id is written back to the selected
    # point of sale, so this exposes pos.config.is_pharmacy in the POS settings panel.
    pos_is_pharmacy = fields.Boolean(
        related='pos_config_id.is_pharmacy', readonly=False)
