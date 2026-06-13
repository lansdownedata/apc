# All Pro Charter — Payments, Deposits & Revenue Recognition · Design Spec

**Prepared:** 2026-06-13 · for All Pro Charter (Lansdowne Data)
**Status:** Approved direction — pending spec review
**Builds on:** `2026-06-09-lead-manager-portal-design.md`, `2026-06-10-lead-manager-erd.md`, `2026-06-10-django-app-scope.md`
**Touches apps:** `payments` (core), `leads`, `reservations`, `messaging`, `notifications`, `core`, `portal`

---

## 1. Purpose

The deposit/balance **mechanics** already exist (`PaymentPlan`, `Charge`, Stripe deposit
Checkout + off-session balance charge + failure alert). This spec adds the **accounting
layer** on top of them so the business can track every dollar professionally:

- A real **double-entry ledger** for all deposits, balances, refunds, and revenue.
- **Deferred revenue** treatment: prepaid cash is a liability until a trip is performed.
- **Per-trip revenue recognition**: revenue is earned when a trip's pickup date has passed
  and the trip is in an earned-terminal status.
- An **Orders & Order-Payments review surface** for staff.
- **Payment touchpoints** (deposit request, balance heads-up, receipts) via Podium.

### Goals
- Books that reconcile: at any moment, `collected = deferred + recognized + refunded (± A/R)`.
- GAAP-aligned recognition (ASC 606 spirit): recognize revenue when the performance
  obligation — the individual trip — is satisfied.
- An immutable, auditable trail of every money and revenue event.
- A staff-facing place to review orders, their payments, and their ledger, and to act
  (retry balance, refund, forfeit deposit, recognize/reverse).

### Non-goals
- No configurable chart of accounts / general-ledger software (accounts are a fixed enum).
- No automated cancellation **fee schedule** in v1 (cancellations are handled manually;
  the ledger supports a schedule being added later with no model changes).
- No automated customer-facing *failed-charge* message — a declined balance is an internal
  admin matter (existing `Notification`) and surfaces on the books as A/R (see §5, §8).
- Not changing how Stripe captures money; this layers accounting onto the existing flow.

### Confirmed decisions
- **Accounting model:** double-entry event ledger.
- **Recognition:** automatic, per-trip, nightly.
- **Cancellations/refunds:** manual, ledger-backed.
- **Touchpoints:** deposit request on quote · balance charge heads-up · payment receipts
  (no automated failed-charge follow-up).
- **`no_show` is earned revenue** (the vehicle was provided).
- **Orders console is visible to all authenticated users.**

---

## 2. Framing: the order is the booked quote

A `Lead` *is* the quote, and it holds many `Reservation`s (trips). We keep that — **a booked
`Lead` = an Order** — rather than introduce a parallel Order entity. Money lives at the order
level; **revenue is earned at the trip level**. That split drives everything below.

- **Order identifier:** the existing `Lead.quote_no` (`Q-####`) doubles as the order number.
- **Order total:** `Lead.quote_total` = Σ `Reservation.line_total`.
- **Trip revenue:** each `Reservation.line_total` is that trip's share of revenue.

---

## 3. The accounting model — double-entry ledger

### 3.1 Chart of accounts (fixed `core` enum, not a table)

| Code | Account | Type | Normal balance | Meaning |
|---|---|---|---|---|
| `cash` | Cash / Stripe Clearing | Asset | Debit | Money received via Stripe |
| `customer_deposits` | Customer Deposits | Liability | Credit | Prepaid cash **not yet earned** (deferred revenue) |
| `accounts_receivable` | Accounts Receivable | Asset | Debit | Trip earned but **not yet collected** |
| `recognized_revenue` | Recognized Revenue | Income | Credit | Earned trip revenue |
| `cancellation_revenue` | Cancellation Revenue | Income | Credit | Forfeited deposits |
| `refunds` | Refunds | Contra-income | Debit | Cash returned after revenue was earned |
| `processing_fees` | Processing Fees | Expense | Debit | Stripe fees (Phase 4 — needs balance-txn fetch) |

### 3.2 Entries and immutability

- Every money or revenue event is **one balanced `JournalEntry`** composed of ≥2
  `JournalLine`s where `Σ debits == Σ credits`.
- Entries are **immutable** once posted. Corrections are **reversing entries**, never edits
  or deletes. This is the audit trail.
- Each entry carries an **`idempotency_key`** (unique) so a webhook retry or a re-run of the
  recognition job cannot double-post (mirrors `Charge.idempotency_key`).

### 3.3 Derived balances

```
account_balance        = Σ debits − Σ credits        (filterable per order via entry.lead)
order.collected        = Σ cash debits for the order
order.deferred         = customer_deposits balance    (collected − recognized − refunded so far)
order.recognized       = recognized_revenue balance
order.accounts_receiv. = accounts_receivable balance  (> 0 ⇒ customer owes us)
```

v1 computes these by aggregation. If the Orders console gets slow, denormalize cached
totals onto `PaymentPlan`, updated whenever an entry posts (noted, not built in Phase 1).

---

## 4. Lifecycle in journal entries

Concrete 2-trip order — Trip A **$1,500** (Jun 20), Trip B **$1,170** (Jul 10), total **$2,670**:

| When | Event | Debit | Credit | Deferred | Recognized |
|---|---|---|---|---|---|
| Booking | Deposit captured $1,335 | Cash 1,335 | Customer Deposits 1,335 | 1,335 | 0 |
| ~May 21 (30d pre-trip) | Balance captured $1,335 | Cash 1,335 | Customer Deposits 1,335 | 2,670 | 0 |
| Night of Jun 20 | Trip A done → recognize | Customer Deposits 1,500 | Recognized Revenue 1,500 | 1,170 | 1,500 |
| Night of Jul 10 | Trip B done → recognize | Customer Deposits 1,170 | Recognized Revenue 1,170 | 0 | 2,670 |

### 4.1 Posting rules

**Cash capture** (deposit or balance), amount `c` — clears A/R before adding to deferred:
```
to_ar       = min(c, order.accounts_receivable)
to_deferred = c − to_ar
Dr Cash c   |   Cr Accounts Receivable to_ar   |   Cr Customer Deposits to_deferred
```
(Normally A/R is 0 at capture time, so this is just `Dr Cash / Cr Customer Deposits`.)

**Revenue recognition** for a trip, amount `r` — draws deferred first, overflow to A/R:
```
from_deferred = min(r, order.deferred)
to_ar         = r − from_deferred
Dr Customer Deposits from_deferred  |  Dr Accounts Receivable to_ar  |  Cr Recognized Revenue r
```

**Failed balance, trip still runs** — the case behind the failure requirement. Only the
$1,335 deposit is in; recognizing Trip A ($1,500) overflows $165 into A/R:
```
Dr Customer Deposits 1,335 + Dr Accounts Receivable 165   |   Cr Recognized Revenue 1,500
```
So a declined balance isn't just an alert — it shows up as **money owed (A/R)** on the books.
When the balance is later recovered, the capture rule clears that A/R first.

**Refund**, amount `x` (manual) — unearned cash reverses deferred; post-earning reverses revenue:
```
from_deferred = min(x, order.deferred)
Dr Customer Deposits from_deferred  |  Dr Refunds (x − from_deferred)  |  Cr Cash x
```

**Forfeited deposit** (cancellation, keep the money), amount `f` — reclassify, no cash moves:
```
Dr Customer Deposits f   |   Cr Cancellation Revenue f
```

---

## 5. Revenue recognition (automatic, per-trip, nightly)

- New Celery-beat task **`recognize_due_revenue`**, scheduled daily alongside the existing
  `charge_due_balances`.
- Selects `Reservation`s where **`pickup_date < today`** AND **`trip_status ∈ EARNED_TERMINAL`**
  AND **`revenue_status == deferred`**.
- Posts the recognition entry (§4.1), then sets `revenue_status = recognized`,
  `recognized_at = now`, `recognized_amount = line_total`. Idempotent via both the
  `revenue_status` guard and the entry `idempotency_key` (`recognize-res{id}`).

### 5.1 Status sets (independent of the dispatch-phase grouping)
- **`EARNED_TERMINAL = {done, no_show}`** → auto-recognized. *(Note: `no_show` maps to the
  "Cancelled" dispatch phase in `TRIP_PHASE_BY_STATUS`, but for revenue it is earned —
  treat it explicitly as earned, not via phase.)*
- **`CANCELLED = {cancelled, cancelled_by_affiliate, late_cancel, covid_cancellation}`** →
  excluded from auto-recognition; handled via the manual refund/forfeit path (§6).
- All other statuses are non-terminal → no recognition yet.

---

## 6. Cancellations & refunds (manual, ledger-backed)

Staff actions on an order, each posting the matching entry from §4.1 and recording state:
- **Issue refund** → Stripe refund (`Refund` via the API) + refund entry. The refund is
  tracked as a `Charge` of kind `refund` with `stripe_refund_id`.
- **Forfeit deposit** → forfeit entry (`Cr Cancellation Revenue`).
- **Mixed** (partial refund + partial forfeit) → both entries.
- Affected `Reservation`s → `revenue_status = reversed`.

A tiered, days-to-pickup fee **schedule** can be layered on later: it would only choose the
refund/forfeit split automatically and call the same posting services — **no model changes**.

---

## 7. Data model changes

### New (`apps/payments`)
- **`JournalEntry`** (`TimeStampedModel`)
  - `lead` FK (the order), `reservation` FK (nullable; set for recognition/reversal),
    `kind` (`deposit_captured` · `balance_captured` · `revenue_recognized` ·
    `refund_issued` · `deposit_forfeited` · `reversal` · `adjustment`),
    `source` (`stripe` · `system` · `manual`), `memo`,
    `charge` FK (nullable), `stripe_ref` (blank), `created_by` FK (nullable),
    `idempotency_key` (unique), `posted_at`.
  - Invariant enforced on save/post: lines balance (`Σ debit == Σ credit`).
- **`JournalLine`** (`TimeStampedModel`)
  - `entry` FK, `account` (the §3.1 enum), `debit` `MoneyField` (default 0),
    `credit` `MoneyField` (default 0). Exactly one of debit/credit is non-zero.

### Extended
- **`Charge`** — add `REFUND` to `Kind`; add `stripe_refund_id`. Every successful
  `Charge`/refund posts a `JournalEntry` (capture or refund).
- **`Reservation`** — add `revenue_status` (`deferred` → `recognized` → `reversed`),
  `recognized_at` (datetime, null), `recognized_amount` (`MoneyField`, default 0).
- **`PaymentPlan`** — unchanged in shape; remains the per-order money **summary/config**
  (deposit %, card on file, cached deposit/balance statuses). **The ledger is the source of
  truth**; plan balances are derived. (Optional later: cached `deferred`/`recognized`/`ar`.)

### Core
- **`apps/core/choices.py`** — add the `Account` `TextChoices` enum (§3.1).

### Services & jobs (no side-effects in views)
- `apps/payments/ledger.py` — `post_entry(...)`, `order_balances(lead)`, and the posting
  helpers for capture / recognize / refund / forfeit / reverse (encapsulate §4.1 rules).
- `apps/payments/webhooks.py` — on deposit/balance success, call the capture poster.
- `apps/payments/tasks.py` — add `recognize_due_revenue` (+ beat schedule entry).

---

## 8. Admin review surface ("orders & order payments")

All views are `@login_required` and **visible to every authenticated user**. (Destructive
financial actions — refund, forfeit, mark-paid — are unrestricted in v1 but written so they
can be gated to `owner_admin` later via a single decorator/flag.)

- **Orders console** — `apps/payments` (views/urls), route `/orders/`, surfaced in the
  portal nav. Columns:
  order # · customer · trips · **order total · collected · deferred · recognized · A/R** ·
  payment state · next action (balance date / **failed**). Filters: balance-due, **failed**,
  partially recognized, fully earned. Reuses the existing table/badge/searchable-select
  components.
- **Order-payments detail** — `/orders/<lead_id>/`. The money view for one order:
  payment-plan summary; the **ledger** (entries with debit/credit lines + running deferred
  balance); charges & refunds; per-trip recognition status; and **actions** (retry balance,
  issue refund, forfeit deposit, mark paid, recognize/reverse a trip) — all through the
  reusable **modal**, all posting journal entries, with a **toast** on success.
- **Finance summary** — a dashboard widget: total **deferred liability**, **recognized
  revenue** (month-to-date), **A/R outstanding**, **refunds**. This is the at-a-glance "track
  all deposits professionally" view.

---

## 9. Payment touchpoints (Podium)

New `TouchPoint.Kind`s, woven into the existing `pretrip_reminder` + `review_request`:

```
Quote sent ──DEPOSIT_REQUEST──▶ … ──BALANCE_REMINDER──▶ [auto-charge 30d pre-trip]
   │                                  (a few days before)
   └ deposit paid ──PAYMENT_RECEIPT       balance paid ──PAYMENT_RECEIPT
                                                              │
                                          [trip] ──PRETRIP_REMINDER──▶ … ──REVIEW_REQUEST
```

- `DEPOSIT_REQUEST` — scheduled/sent when the quote is sent (carries the deposit link/ask).
- `BALANCE_REMINDER` — scheduled for `balance_due_date − 3 days` (configurable via
  `BALANCE_REMINDER_DAYS_BEFORE`); sent by the scheduler.
- `PAYMENT_RECEIPT` — **event-driven**: fires on the Stripe webhook for deposit and balance
  success.
- All are logged as `TouchPoint` rows for one unified comms history. The send mechanism
  reuses the planned touch-point scheduler + Podium client.

---

## 10. Requirements traceability

| Requirement | Mechanism |
|---|---|
| Quote goes out with a 50% deposit request | `DEPOSIT_REQUEST` touchpoint + deposit Checkout (exists) |
| Auto-book on deposit | existing webhook → `Lead.BOOKED` |
| Charge balance 30 days before pickup | existing `charge_due_balances` + `BALANCE_REMINDER` |
| Failed charge → notify on the lead | existing `Notification` **+** A/R appears on the books |
| Revenue prepaid until PU date past & terminal, then recognized | deferred ledger + per-trip nightly recognition (`done`, `no_show`) |
| Track all deposits professionally / accounting best practices | double-entry immutable ledger + derived balances |
| Place to review orders & order payments | Orders console + order-payments detail + ledger + finance summary |
| Touchpoints | deposit request · balance heads-up · receipts (+ existing reminder/review) |

---

## 11. Build phases

1. **Accounting core** — `Account` enum · `JournalEntry`/`JournalLine` · `ledger.py` posting
   services · wire deposit/balance webhooks to post capture entries · `Reservation`
   revenue fields · `recognize_due_revenue` nightly task. (All TDD.)
2. **Review surface** — Orders console · order-payments detail · ledger view · actions
   (retry/refund/forfeit/mark-paid/recognize/reverse) via the reusable modal.
3. **Touchpoints** — `DEPOSIT_REQUEST` / `BALANCE_REMINDER` / `PAYMENT_RECEIPT` kinds + send
   wiring (depends on the touch-point scheduler).
4. **Finance polish** — finance summary dashboard · Stripe **processing-fee** capture
   (balance-transaction fetch) · (later) automated cancellation fee schedule · optional
   role-gating of destructive actions · cached order balances if needed for scale.

---

## 12. Testing strategy (TDD)

Cover the **logic**, not Stripe:
- **Ledger invariant:** every posted entry balances; unbalanced posts raise.
- **Posting rules:** capture (with/without existing A/R), recognition (deferred-only and
  deferred+A/R overflow), refund (pre- and post-recognition), forfeit, reversal.
- **Order balances:** `collected / deferred / recognized / A/R` after each event in the §4
  lifecycle, including the failed-balance-then-trip-runs and later-recovery sequences.
- **Recognition job:** picks only `pickup_date < today` + `EARNED_TERMINAL` + `deferred`;
  recognizes `done` and `no_show`; skips cancelled and non-terminal; **idempotent** on re-run.
- **Idempotency:** duplicate webhook / re-run posts no second entry.
- **Views:** Orders console + detail are `login_required` and reachable by a non-admin user;
  actions post the expected entries and update state.

**Definition of done** (per the repo): tests first and green · ruff clean · `manage.py check`
clean · migrations committed · reused modal/searchable-select (no native dialogs/selects) ·
admin registered where useful.

---

## 13. Open questions / future
- Stripe **processing fees**: capture net vs gross (Phase 4 fetches the balance transaction).
- Period **close/lock**: do we ever freeze a month so entries can't post into it? (Future.)
- **Cancellation fee schedule**: exact tiers by days-to-pickup, when the business defines them.
- **Role-gating**: whether refund/forfeit should later require `owner_admin`.
