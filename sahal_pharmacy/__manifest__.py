# -*- coding: utf-8 -*-
{
    'name': 'Pharmacy Management',
    'version': '19.0.3.0.1',  # Pharmacy fields no longer break non-pharmacy product screens
    'summary': 'Retail and wholesale pharmacy: medicines, supplements, prescriptions, '
               'suppliers, wholesale credit control and expiry management',
    'description': """
Pharmacy Management for retail pharmacies, wholesalers and chains.

Built ON TOP of native Odoo rather than beside it: medicines are products, patients,
doctors, suppliers and wholesale customers are contacts, and batches are stock lots.
That is what lets Inventory, Purchase, Sales, POS and Accounting work on pharmacy data
without a parallel stack.

RETAIL
  * Medicine catalogue - product classification (prescription / OTC / supplement /
    medical device / cosmetic), generic-to-brand relations, active ingredients, dosage
    forms and routes as configurable records, structured strength, ATC and therapeutic
    class, GTIN, country of origin, antibiotic / high-alert / hazardous / pediatric /
    veterinary flags, cold-chain temperature range
  * Patient clinical profile - allergies, chronic conditions, insurance, emergency contact
  * Prescriptions - pharmacist verification, validity, refills, partial dispensing
  * Dispensing - creates a real sale order and moves stock, with batch capture
  * Controlled drugs - restricted register, every movement logged, disposal witnessed
  * Expiry - near-expiry alerts, expired stock blocked from dispensing, expiry as a
    reportable dimension with stock value attached

WHOLESALE
  * Wholesale customers - trading licence and expiry, customer category, approval to buy
    medicines, price list per category
  * Credit control - Odoo credit limits plus a credit HOLD that only a manager releases
  * Order quantities - minimum, maximum and whole-case multiples enforced on the order
  * Licence, credit and quantity checks on confirmation, with a recorded manager override
  * Licensed wholesale supply is exempt from the retail prescription rule: a wholesaler
    supplying a licensed pharmacy is not dispensing to a patient

SUPPLIERS
  * Pharmaceutical licence with expiry and issuing authority, supplier category
  * Approved-supplier status, and approval per product on the vendor pricelist line
  * Purchase orders warn or block when the vendor is unapproved or unlicensed
  * Returns to supplier - reason, batch and approval, raising the return shipment and
    the vendor credit note

CLINICAL SAFETY
  * Allergy screening matched on the ACTIVE INGREDIENT, so a brand nobody typed is still
    caught, with free-text allergies matched through ingredient aliases
  * Drug interactions with severity and recommended action, checked across the script
  * High and critical findings must be signed off by a named pharmacist before a
    prescription can be verified or dispensed
  * Substitution records - what was swapped, why, by whom, and whether it was actually
    the same generic

DISPENSING CONTROL
  * Expired stock refused at the till and at the counter, not merely warned about,
    with a recorded pharmacist override where policy allows one
  * FEFO batch suggestion on the dispensing screen: the earliest-expiring usable stock
  * Customer returns with the batch, the condition it came back in, and a pharmacist's
    decision on restock or destruction - separate from whether the customer is refunded
  * Insurance claims: the copay split, the claim's life with the insurer, and the
    insurer invoiced only once they have agreed

COMPLIANCE
  * Pharmacy profile per branch - licence, expiry, pharmacist in charge, hours
  * Manufacturers as licensed records with GMP certificates and approval
  * Regulatory documents with expiry alerts
  * One list of every authorised override, with who allowed it and why

STOCK INTELLIGENCE
  * Out of stock, low stock, dead stock and expiry loss, with a daily internal digest
    to pharmacy managers. Nothing here contacts a customer.

Deliberately NOT reimplemented (handled by native Odoo):
  warehouses/bins, lot tracking, FEFO (product_expiry), transfers, adjustments,
  purchasing, vendor bills, invoicing, taxes, inventory valuation, pricelists,
  credit limits and POS payments.

For hospital dispensaries see inom_healthcare_system; this module targets

Install and usage instructions: see README.md in this module's directory.
    """,
    'price': 289.00,
    'currency': 'USD',
    'support': 'support@bandtsolutions.com',
    'images': ['static/description/banner.png'],
    'category': 'Industries/Pharmacy',
    'author': 'B&T Solutions',
    'website': 'https://bandtsolutions.com',
    'license': 'OPL-1',
    # product_expiry is what gives lots an expiration date and enables the FEFO
    # removal strategy - the backbone of pharmacy stock rotation. Depending on it
    # installs it automatically rather than leaving it to manual configuration.
    'depends': [
        'base',
        'mail',
        'product',
        'stock',
        'stock_account',
        'product_expiry',
        'sale_management',
        'purchase',
        'account',
        'barcodes',
        'point_of_sale',
    ],
    'assets': {
        # POS bundle: the till patch ONLY. This bundle is downloaded by every point of
        # sale on the platform - restaurants, hotels, hardware shops - so nothing else
        # belongs in it.
        'point_of_sale._assets_pos': [
            'sahal_pharmacy/static/src/js/pos_pharmacy.js',
        ],
        # Back office: the dashboard. Never loaded by a till.
        'web.assets_backend': [
            'sahal_pharmacy/static/src/css/pharmacy_dashboard.css',
            'sahal_pharmacy/static/src/js/pharmacy_dashboard.js',
            'sahal_pharmacy/static/src/xml/pharmacy_dashboard.xml',
        ],
    },
    'data': [
        'security/pharmacy_groups.xml',
        'security/ir.model.access.csv',
        'security/pharmacy_rules.xml',
        'data/pharmacy_sequences.xml',
        'data/pharmacy_catalog_data.xml',
        'data/pharmacy_pricelist_data.xml',
        'data/pharmacy_data.xml',
        'data/pharmacy_cron.xml',
        'views/pharmacy_catalog_views.xml',
        # product_template_views.xml defines product_template_form_pharmacy, which
        # pharmacy_wholesale_views.xml and res_partner_views.xml extend by xmlid. It has
        # to load FIRST. On an already-installed database the id is in ir_model_data
        # from a previous version so any order appears to work - the failure only shows
        # on a FRESH install, which is what every buyer and every disaster recovery does.
        'views/product_template_views.xml',
        'views/res_partner_views.xml',
        'views/pharmacy_supplier_views.xml',
        'views/pharmacy_wholesale_views.xml',
        # Base forms before the files that extend them by xmlid. Verified against the
        # actual inherit_id references, not by trial: prescription and dispense forms are
        # extended by the clinical and insurance views respectively.
        'views/pharmacy_prescription_views.xml',
        'views/pharmacy_dispense_views.xml',
        'views/pharmacy_clinical_views.xml',
        'views/pharmacy_return_views.xml',
        'views/pharmacy_compliance_views.xml',
        'views/pharmacy_insurance_views.xml',
        'views/pharmacy_stock_views.xml',
        'views/pharmacy_reporting_views.xml',
        'views/pharmacy_controlled_views.xml',
        'views/pharmacy_menus.xml',
        'views/pharmacy_dashboard_views.xml',
        'views/pos_settings_views.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'installable': True,
    'application': True,
    'auto_install': False,
}
