# Utility Management (`utility_management`)

Customer information and revenue management for **water, electricity, gas and internet**
providers: service accounts, meters, readings, tariffs, billing runs, bills, statements
and customer notifications.

Built on native Odoo. Service accounts, meters, readings, tariffs and billing runs are the
custom models; **billing posts real `account.move` invoices with real taxes**, and
customers are ordinary contacts — so Accounting, payments and reporting work on utility
data without a parallel ledger.

---

## 1. Installing

### Requirements

Odoo 19 with `account` configured — a chart of accounts, a sales journal and your tax
rates. Billing posts real invoices, so this must be right before the first run.

### Install

1. Copy the `utility_management` folder into your Odoo **addons path**.
2. Restart the Odoo service.
3. **Apps → Update Apps List**, search for the module and click **Install**.

Or from the command line:

```bash
# new install
odoo -d <database> -i utility_management --stop-after-init

# upgrade
odoo -d <database> -u utility_management --stop-after-init
```

### What installation does for you

* Creates the three roles (below) and grants **Utility Manager** to existing system
  administrators, so the app is not invisible on first install.
* Loads sequences for accounts, readings, billing runs and bills.
* Installs the five customer mail templates: invoice issued, payment received, overdue
  notice, meter-reading reminder, estimated-bill notice.
* Installs one scheduled action, **Utilities: Generate Due Bills**, which runs daily and
  creates **draft** invoices for accounts whose cycle is due.

> ### ⚠ Before going live, decide about the billing cron
> **"Utilities: Generate Due Bills" is active on install and creates invoices.** It only
> ever creates *drafts*, and it contacts nobody — but on a database with real customers you
> should confirm your tariffs and cycles first. To hold it: **Settings → Technical →
> Scheduled Actions → Utilities: Generate Due Bills → untick Active.**
>
> The mail templates do **not** send on their own; sending is an explicit action.

### Verify the install

```bash
odoo shell -d <database> --no-http --logfile=/dev/null \
  < addons/utility_management/tests/p1_service_account_check.py
```

Six suites (`p1`–`p6`) cover service accounts, reading validation, tariff charges,
billing runs, reports and dashboard, and notifications. Each rolls its
transaction back, so they are safe on a working database.

---

## 2. Roles

Assign under **Settings → Users → *user* → Utility Management**.

| Role | Can do |
|---|---|
| **Field Technician** | Record meter readings, install and replace meters. |
| **Billing Specialist** | Everything above, plus review reading exceptions, run billing, issue invoices and statements. |
| **Utility Manager** | Full access — tariffs, billing cycles, approving runs, corrections and write-offs. |

---

## 3. Getting started

Do these once, in this order. Each depends on the one before it.

### 3.1 Create a tariff

**Utilities → Configuration → Tariffs → New**.

* Choose the utility type and whether the tariff is **flat** or **tiered**.
* For tiered, add blocks with an explicit lower and upper limit. Blocks must be
  contiguous and must not overlap — the system refuses a set that would mis-bill.
* Add a standing/base charge and the tax to apply.
* Add any **fees, penalties, discounts, subsidies, minimum or maximum charges** as tariff
  charge lines, so local pricing rules stay configuration rather than code.

### 3.2 Create a billing cycle

**Utilities → Configuration → Billing Cycles → New**. Set the frequency, the day the
period anchors on, and how many days after issue the bill is due.

### 3.3 Create a service account

The service account — not the customer, and not the meter — is the spine of the system. A
customer can hold several: electricity and water, or two premises.

**Utilities → Operations → Service Accounts → New**:

1. Choose the customer and the utility type. The account number is issued automatically.
2. Enter the **service location** (street, city, area, and coordinates if you have them).
3. Set the **tariff** and **billing cycle**.
4. Record the connection date and any **deposit**.
5. Set the status: *Pending → Active*, and later *Suspended*, *Disconnected*, *Closed* or
   *Transferred*.

### 3.4 Install the meter

**Utilities → Operations → Meters → New**. Record the serial number, type, manufacturer,
model, installation date, opening reading and — importantly — the **number of digits**,
which is what makes rollover detection possible.

Attach it to the service account. That creates the opening **installation record**, which
is the meter's history: who fitted it, when, at what reading, and why.

---

## 4. Everyday work

### Recording readings

**Utilities → Operations → Meter Readings → New**. Choose the meter and enter the reading
and date. Consumption is `(present − previous) × multiplier`, calculated for you.

Every reading is validated on save, and flagged rather than refused when something looks
wrong:

| Status | Means |
|---|---|
| **Normal** | Nothing unusual. |
| **Review Required** | Abnormally high or low, zero consumption, a duplicate, or read outside the expected window. |
| **Estimated** | Not an actual read — see below. |
| **Invalid** | Cannot be used for billing. |
| **Suspected Tampering** | The pattern suggests interference. |

> **A reading lower than the last one is recorded, not rejected.** Rollover, a replaced
> meter and a mis-keyed dial are all real; refusing to store the reading only means the
> field team writes it on paper. With the meter's digit count set, rollover is calculated
> properly as `(10^digits − previous + present) × multiplier` and flagged on the bill.

Exceptions are reviewed under **Utilities → Operations → Reading Exceptions**. Only Billing
and Manager roles can approve or correct one, and every correction is tracked.

### Estimated readings and true-up

When a meter cannot be read, the account's **estimation method** is used: previous period,
3-month average, 6-month average, same period last year, or a configured minimum.

When a real reading finally arrives after estimated bills have been **issued**, the
difference is carried onto the next bill as an explicit adjustment line referencing the
invoices it corrects. Estimated bills are labelled as such on the PDF.

### Replacing a meter

Open the meter and click **Replace Meter** rather than editing the record. The wizard closes the old
meter at its final reading and opens the new one at its initial reading in one step, so
consumption continuity is preserved and the history shows both.

---

## 5. Billing

Billing runs through a **billing run**, not straight from readings — so a bad run is
caught before invoices exist.

1. **Utilities → Operations → Billing Runs → New**. Choose the cycle and the period.
2. **Compute.** One line per service account: readings used, consumption, whether it was
   estimated, the charge breakdown and the amount.
3. **Review.** The run compares itself against the previous one and warns about billing
   down more than 30%, unusual consumption, a high count of estimates, zero-consumption
   meters, and a high count of adjustments.
4. **Approve.** The period locks — its readings can no longer be edited.
5. **Invoice.** Real `account.move` invoices are created, dated at period end, with due
   dates from the payment terms.

Recomputing a draft run is **idempotent**: the same inputs give the same lines and no
duplicate invoices. One line per account per run means an account cannot be billed twice.

> The **Generate Invoices** menu and the daily cron are conveniences that create a run
> behind the scenes. All three paths produce identical output.

### Payments

Register payments the normal Odoo way — cash, bank, card, cheque or mobile money. Nothing
utility-specific is needed, and the account balance follows.

---

## 6. Documents

* **Utility bill (PDF)** — account, period, previous and current reading, consumption,
  tariff breakdown, fixed charges, taxes, previous balance and amount due.
* **Account statement (PDF)** — the ledger: date, transaction, debit, credit, running
  balance. Printed from a **service account** (Print → Account Statement). A customer
  holding several accounts gets one statement per account, which is usually what they
  want — electricity and water are separate bills.

---

## 7. Notifications

Five templates ship: **invoice issued, payment received, overdue notice, meter-reading
reminder, estimated-bill notice**. Each account carries a channel preference, and every
message sent is recorded in the notification log.

> ### WhatsApp — read before enabling
> The WhatsApp channel is **inert until you configure credentials**, and deliberately so.
> Automated WhatsApp requires the **Meta WhatsApp Cloud API**: a Meta Business account, a
> verified sender number, and message templates pre-approved by Meta. The separate
> click-to-chat addon on this platform cannot do it — it only builds links a human must
> press.
>
> Set these under **Settings → Technical → System Parameters** once you have them:
>
> | Parameter | Value |
> |---|---|
> | `utility_management.whatsapp_api_url` | Cloud API endpoint |
> | `utility_management.whatsapp_phone_id` | Verified sender's phone number ID |
> | `utility_management.whatsapp_token` | Permanent access token |
> | `utility_management.whatsapp_language` | Template language code, e.g. `en` |
>
> **No WhatsApp traffic leaves the system until all four are set.**

---

## 8. Reporting

**Utilities → Reporting** carries consumption analysis, billing analysis, out-of-window
readings and revenue views. The **dashboard** adds KPIs — billing completion, reading
completion, percentage estimated, collection rate and receivables ageing — with filters by
date, utility, area, customer type, tariff and status.

The dashboard reports **only what the signed-in user may see**; it does not elevate its own
queries, so a KPI can never total something the user cannot open.

---

## 9. Not built yet

Deliberately out of scope for this round, so nobody goes looking:

* **Customer portal** — customers cannot yet see their own bills or usage online.
* **Field/mobile reading** — no routes, assignments, offline capture or GPS-stamped reads.
* **Collections, disconnection and reconnection** — `om_account_followup` gives basic
  dunning as a stopgap.
* **Service requests and complaints** — the helpdesk module can back this meanwhile.
* **GIS** — coordinates are stored but nothing consumes them yet: no map view or
  consumption map.
* **REST/OpenAPI** — Odoo exposes XML-RPC and JSON-RPC only.

---

## 10. Support

* **User guide (PDF):** `docs/user_guides/11_Utility.pdf` — written for staff, not developers.
