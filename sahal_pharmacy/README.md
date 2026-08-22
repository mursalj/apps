# Pharmacy Management (`sahal_pharmacy`)

Retail and wholesale pharmacy on Odoo 19: medicines, supplements, prescriptions,
dispensing, suppliers, wholesale credit control, controlled drugs, expiry and insurance.

Built **on top of** native Odoo rather than beside it — medicines are products, patients
and suppliers are contacts, batches are stock lots. That is what lets Inventory, Purchase,
Sales, POS and Accounting work on pharmacy data without a parallel stack.

> For a **hospital dispensary**, use `inom_healthcare_system` instead. It has its own
> formulary that sits on this module's product stack; the two share one inventory and one
> controlled-drugs register.

---

## 1. Installing

### Requirements

Odoo 19 with `stock`, `purchase`, `sale_management`, `account`, `point_of_sale` and
`barcodes` available. `product_expiry` is pulled in automatically — it is what gives lots
an expiration date and enables FEFO removal, which is the backbone of stock rotation here.

### Install

1. Copy the `sahal_pharmacy` folder into your Odoo **addons path**.
2. Restart the Odoo service.
3. **Apps → Update Apps List**, search for the module and click **Install**.

Or from the command line:

```bash
odoo -d <database> -i sahal_pharmacy --stop-after-init
```

**Upgrading an existing install:**

```bash
odoo -d <database> -u sahal_pharmacy --stop-after-init
```

### What installation does for you

* Creates the five roles (below) and grants **Manager** to existing system administrators,
  so the app is not invisible the moment it is installed.
* Loads dosage forms, routes and a starter catalogue of active ingredients.
* Creates the sequences for prescriptions, dispensings and returns.
* Installs five scheduled actions. **All are internal** — they email pharmacy staff, never
  a customer:

  | Cron | Does |
  |---|---|
  | Pharmacy: Expiry Alerts | Flags lots approaching expiry |
  | Pharmacy: Expire Old Prescriptions | Closes prescriptions past their validity |
  | Pharmacy: Supplier Licence Expiry Alerts | Warns before a supplier's licence lapses |
  | Pharmacy: Regulatory Document Expiry Alerts | Warns before a permit lapses |
  | Pharmacy: Stock Alerts | Daily out-of-stock / low / dead-stock digest |

### Verify the install

```bash
addons/sahal_pharmacy/tests/  # 12 suites
odoo shell -d <database> --no-http --logfile=/dev/null \
  < addons/sahal_pharmacy/tests/ph1_product_master_check.py
```

Each suite rolls its transaction back, so this is safe on a working database.

---

## 2. Roles

Assign under **Settings → Users → *user* → Pharmacy**. Roles build on each other — a
Pharmacist can do everything a Technician and Cashier can.

| Role | Can do |
|---|---|
| **Cashier** | Sell OTC medicines and take payment. **Cannot** verify prescriptions or handle controlled drugs. |
| **Technician** | Everything above, plus prepare and dispense non-controlled prescriptions. |
| **Pharmacist** | Verify prescriptions, sign off clinical warnings, dispense controlled drugs and sign the register. |
| **Manager** | Full access — set up medicines, release credit holds, authorise overrides, run reports. |
| **Auditor** | Read-only across the register, dispensing history and the override log, for compliance checks. |

> **Creating a cashier-only user:** give them **Cashier** and nothing else. They will see
> the till and the product catalogue but not the register, and any attempt to ring up a
> prescription-only medicine is refused at the till *and* on the server.

---

## 3. Getting started

Do these once, in this order.

### 3.1 Add a medicine

Medicines are products, so anything added here can be purchased, stocked, sold and
invoiced like any product — with the pharmaceutical detail on top.

1. **Pharmacy → Catalogue → Medicines → New**.
2. Set **Pharma Type**: Prescription, OTC, Supplement, Medical Device or Cosmetic. This is
   what drives the selling rules.
3. Tick **Requires Prescription** and/or **Controlled** where they apply.
4. Set **Tracking = By Lots** and **Expiration Date** on the Inventory tab. Without a lot,
   there is no batch and no expiry control.
5. Fill in generic name, strength, dosage form and route so substitution and allergy
   screening work.

### 3.2 Receive stock with batches

1. **Purchase → New**, choosing an **approved, licensed** supplier (see §5).
2. Receive the shipment. On the receipt, enter the **lot number and expiry date** for each
   medicine.
3. Validate. The batch is now dispensable, and FEFO will offer it in expiry order.

### 3.3 Set up the till

1. **Point of Sale → Configuration → Point of Sale**, open the till.
2. Tick **Pharmacy Point of Sale** (needs the Manager role to see it).

Only a till with this ticked loads pharmacy data or pharmacy behaviour. Every other POS on
the platform — restaurant, hotel, hardware — is completely unaffected.

---

## 4. Everyday retail

### Selling over the counter

Ring the sale up as normal. The till refuses a prescription-only medicine unless a
verified prescription is attached, warns on near-expiry batches, and blocks expired stock.

### Dispensing a prescription

1. **Pharmacy → Dispensing → Prescriptions → New**. Add the patient, prescriber, validity and each
   medicine with dose and quantity.
2. Click **Check Clinical Safety**. Allergies are matched on the **active ingredient**, so
   a brand nobody typed is still caught, and interactions are checked across the whole
   script.
3. **High and critical findings must be signed off by a named pharmacist** before the
   prescription can be verified.
4. **Verify** (Pharmacist only), then **Dispense**. Dispensing creates a real sale order,
   moves real stock, and captures the batch handed over.

Partial dispensing and refills are supported — the remaining quantity stays on the script.

### Controlled drugs

Every movement writes to the register automatically. A pharmacist must be the one to hand
them over, and disposal requires a witness. **Pharmacy → Compliance → Controlled Drugs Register** is the
report to print for an inspection.

### Customer returns

**Pharmacy → Customer Returns → New**. Record the batch and the condition it came back in. The
pharmacist's decision to **restock or destroy** is separate from whether the customer is
refunded — a medicine that has left the premises usually cannot go back on the shelf even
when the refund is legitimate.

---

## 5. Suppliers and wholesale

### Suppliers

Record the **pharmaceutical licence, its expiry and the issuing authority** on the contact,
and mark the supplier **Approved**. Purchase orders warn or block for an unapproved or
unlicensed vendor, per the enforcement setting.

### Wholesale customers

On the customer, set the **trading licence**, customer category, and tick **Approved to
buy medicines**. Then:

* **Credit control** — Odoo's credit limit, plus a **credit hold** only a Manager can
  release.
* **Order quantities** — minimum, maximum and whole-case multiples enforced on the order.
* Licence, credit and quantity are all checked on confirmation, with a **recorded manager
  override** where one is allowed.

> Licensed wholesale supply is **exempt** from the retail prescription rule: a wholesaler
> supplying a licensed pharmacy is not dispensing to a patient. An unapproved buyer is
> still blocked, with licence-focused wording.

---

## 6. Expiry

* **FEFO** — the earliest-expiring usable batch is suggested on the dispensing screen.
* **Expired stock is refused**, at the till and at the counter — not merely flagged.
  Policy can allow a recorded pharmacist override.
* **Near-expiry alerts** run daily, and expiry is a reportable dimension with the stock
  value attached, so you can see what it is about to cost you.

---

## 7. The dashboard

**Pharmacy → Dashboard** shows takings today, invoiced this month, receivables, money
outstanding with insurers, colour-coded stock health (out / low / healthy with the shelf's
value), expired and near-expiry lines with their cost, best sellers, sales mix, a 12-month
takings chart, and an attention queue where every row opens the list behind it.

The dashboard reports **only what the signed-in user is allowed to see**. It deliberately
does not elevate its own queries, so a tile can never total something the user
cannot open.

---

## 8. Settings

System parameters under **Settings → Technical → System Parameters**. All have working
defaults; change them only where local policy differs.

| Parameter | Default | Effect |
|---|---|---|
| `sahal_pharmacy.enforce_prescription` | `True` | Refuse prescription-only medicines without a verified script. |
| `sahal_pharmacy.block_expired_sales` | `True` | Refuse expired stock outright rather than warning. |
| `sahal_pharmacy.near_expiry_days` | `90` | How far ahead a batch counts as near-expiry. |
| `sahal_pharmacy.default_shelf_life_days` | `730` | Shelf life assumed when a product does not state one. |
| `sahal_pharmacy.supplier_enforcement` | `warn` | `warn` or `block` for unapproved / unlicensed vendors. |
| `sahal_pharmacy.wholesale_enforcement` | `block` | `warn` or `block` for licence, credit and quantity rules. |
| `sahal_pharmacy.licence_alert_days` | `60` | How far ahead licence expiry is flagged. |
| `sahal_pharmacy.dead_stock_days` | `120` | No movement for this long counts as dead stock. |

---

## 9. Point of sale — the boundary that matters

**One POS serves every industry on this platform.** A restaurant, a hotel shop, a hardware
counter and a pharmacy all run the same `point_of_sale` module and share its asset bundle.
So one rule governs everything here:

> Nothing pharmaceutical reaches a till that is not flagged **Pharmacy Point of Sale**.

That covers the fields loaded, the models loaded and the JavaScript — the client patch
returns immediately on a non-pharmacy till, before touching anything. This is not
theoretical: an unconditional override in this module once broke every POS on the platform.

The one thing **not** gated on the flag is **compliance enforcement**. Whether a
prescription-only medicine may be sold is a legal question about the *product*, not a
preference of the till, so the server checks every order that actually contains restricted
products. Otherwise unticking a checkbox would be a bypass. Orders with no pharmacy
products exit that check immediately.

---

## 10. Deliberately not reimplemented

Handled by native Odoo, and using it directly is the point: warehouses and bins, lot
tracking, FEFO mechanics, transfers, adjustments, purchasing, vendor bills, invoicing,
taxes, inventory valuation, pricelists, credit limits and POS payments.

---

## 11. Support

* **User guide (PDF):** `docs/user_guides/10_Pharmacy.pdf` — written for staff, not developers.
