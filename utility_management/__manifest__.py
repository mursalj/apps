# -*- coding: utf-8 -*-
{
    'name': 'Utility Management',
    'version': '19.0.1.3.0',  # Service accounts, validation, tariff charges, billing runs, bills, notifications
    'summary': 'Service accounts, meters, readings, tariffs, billing runs and bills for utilities',
    'description': """
Utility Customer Information & Revenue Management for water, electricity, gas and
internet providers.

Built on native Odoo - service accounts, meters, readings, tariffs and billing runs are
the custom models; billing posts real account.move invoices with account.tax, and
customers are res.partner.

  * Service accounts - the SRS spine: account number, service location, connection status
    (pending/active/suspended/disconnected/closed/transferred), tariff, billing cycle,
    security deposit, balance and per-account statement. One customer, many connections.
  * Meter registry and lifecycle - serial, dial digits, manufacturer, model, comms type,
    calibration and inspection dates, CT multiplier, photo; installation history with a
    Replace Meter wizard that preserves the old final reading and the new initial one.
  * Reading ingestion and validation - manual entry with photo, reader, condition and GPS;
    the nine SRS checks (negative, low, high, zero, duplicate, missing, out-of-window,
    rollover, tampering) recorded as an exception state with a review workflow, never as a
    refusal to store what the field worker saw.
  * Estimated billing - previous period, 3/6-month average, seasonal or configured
    minimum, with automatic true-up: an over-estimated bill comes back as a credit.
  * Tariff engine - flat, marginal tiered blocks and base charges, plus configurable fees,
    penalties, discounts, subsidies and minimum/maximum charges. No hard-coded rates.
  * Billing runs - draft to review to approved to invoiced, reproducible, duplicate-proof,
    with reading lock on approval and management warnings when billing collapses, estimates
    dominate or exceptions are unresolved.
  * Documents - a real utility bill PDF (readings, tariff breakdown, QR) and a per-account
    customer statement.
  * Notifications - email templates for invoice, payment, overdue, reading reminder and
    estimated bill; WhatsApp through the Meta Cloud API, inert until credentials are set.

The customer self-service portal and the field/mobile application are separate follow-ups.

Install and usage instructions: see README.md in this module's directory.
    """,
    'price': 320.00,
    'currency': 'USD',
    'support': 'support@bandtsolutions.com',
    'images': ['static/description/banner.png'],
    'category': 'Industries/Utilities',
    'author': 'B&T Solutions',
    'website': 'https://bandtsolutions.com',
    'license': 'OPL-1',
    'depends': [
        'base',
        'mail',
        'product',
        'account',
    ],
    'assets': {
        'web.assets_backend': [
            'utility_management/static/src/css/utility_dashboard.css',
            'utility_management/static/src/js/utility_dashboard.js',
            'utility_management/static/src/xml/utility_dashboard.xml',
        ],
    },
    'data': [
        'security/utility_groups.xml',
        'security/ir.model.access.csv',
        'security/utility_rules.xml',
        'data/utility_data.xml',
        'data/utility_mail_templates.xml',
        'views/utility_tariff_views.xml',
        'views/utility_service_account_views.xml',
        'views/utility_meter_views.xml',
        'views/utility_meter_reading_views.xml',
        'views/utility_billing_run_views.xml',
        'wizard/utility_billing_wizard_views.xml',
        'wizard/utility_meter_replace_wizard_views.xml',
        'views/utility_analytics_views.xml',
        'report/utility_bill_report.xml',
        'report/utility_statement_report.xml',
        'views/utility_menus.xml',
        'views/utility_dashboard_views.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'installable': True,
    'application': True,
    'auto_install': False,
}
