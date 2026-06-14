# Payments Accounting Core — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the double-entry ledger that tracks every deposit, balance, refund, and recognized dollar, and recognize each trip's revenue automatically the night after it is performed.

**Architecture:** A small fixed chart of accounts (enum) + immutable `JournalEntry`/`JournalLine` rows. All money/revenue events post balanced entries through one `ledger.py` service. Per-trip revenue recognition runs nightly. The existing Stripe deposit/balance flow is wired to post capture entries. Balances are derived by aggregating lines per account, per order.

**Tech Stack:** Django 5.2, Celery (`@shared_task` + beat), pytest + pytest-django + factory-boy, MySQL. Source spec: `docs/specs/2026-06-13-payments-revenue-accounting-design.md`.

**In scope:** ledger models, posting services (capture/recognize/refund/forfeit), nightly recognition job, webhook capture wiring.
**Out of scope (follow-on plans):** itemized `ReservationCharge` refactor; vendor A/P (`VendorBill`/`VendorCharge`/`VendorPayment`); Orders review surface; payment touchpoints. Recognition uses `reservation.line_total`, so itemization layers in later without touching this plan's code.

---

## File Structure

- `apps/core/choices.py` — **modify**: add the `Account` chart-of-accounts enum.
- `apps/payments/models.py` — **modify**: add `JournalEntry` + `JournalLine`.
- `apps/payments/ledger.py` — **create**: posting service (`post_entry`, `account_balance`, `order_balances`, `post_capture`, `recognize_reservation`, `post_refund`, `post_forfeit`, `LedgerError`).
- `apps/payments/admin.py` — **modify**: register `JournalEntry` with a `JournalLine` inline.
- `apps/payments/tasks.py` — **modify**: add `recognize_due_revenue`.
- `apps/reservations/models.py` — **modify**: add `RevenueStatus` + revenue fields + `EARNED_TERMINAL_STATUSES`.
- `apps/payments/webhooks.py` — **modify**: post a deposit capture entry on `checkout.session.completed`.
- `apps/payments/services.py` — **modify**: post a balance capture entry on a successful balance charge.
- `config/settings/base.py` — **modify**: add the `recognize-due-revenue` beat schedule.
- Tests: `apps/payments/tests/test_ledger.py`, `apps/payments/tests/test_recognition.py`, `apps/payments/tests/test_ledger_wiring.py`, `apps/reservations/tests/test_models.py` (extend).

Run the whole suite with `.venv/bin/python -m pytest -q` (settings preset to dev in `pyproject.toml`).

---

## Task 1: Ledger models (`JournalEntry` + `JournalLine`) and the `Account` enum

**Files:**
- Modify: `apps/core/choices.py`
- Modify: `apps/payments/models.py`
- Modify: `apps/payments/admin.py`
- Test: `apps/payments/tests/test_ledger.py`

- [ ] **Step 1: Add the `Account` enum (config)** — append to `apps/core/choices.py`:

```python
class Account(models.TextChoices):
    """Fixed chart of accounts for the double-entry ledger (see payments spec §3.1)."""

    CASH = "cash", "Cash / Stripe Clearing"
    CUSTOMER_DEPOSITS = "customer_deposits", "Customer Deposits"
    ACCOUNTS_RECEIVABLE = "accounts_receivable", "Accounts Receivable"
    RECOGNIZED_REVENUE = "recognized_revenue", "Recognized Revenue"
    CANCELLATION_REVENUE = "cancellation_revenue", "Cancellation Revenue"
    REFUNDS = "refunds", "Refunds"
    PROCESSING_FEES = "processing_fees", "Processing Fees"
    VENDOR_COST = "vendor_cost", "Vendor Cost"
    VENDOR_PAYABLE = "vendor_payable", "Vendor Payable"
```

- [ ] **Step 2: Write the failing test** — create `apps/payments/tests/test_ledger.py`:

```python
from decimal import Decimal

import pytest

from apps.core.choices import Account
from apps.leads.factories import LeadFactory
from apps.payments.models import JournalEntry, JournalLine

pytestmark = pytest.mark.django_db


def test_journal_entry_balances():
    lead = LeadFactory()
    entry = JournalEntry.objects.create(
        lead=lead, kind=JournalEntry.Kind.DEPOSIT_CAPTURED, idempotency_key="t1"
    )
    JournalLine.objects.create(entry=entry, account=Account.CASH, debit=Decimal("100.00"))
    JournalLine.objects.create(
        entry=entry, account=Account.CUSTOMER_DEPOSITS, credit=Decimal("100.00")
    )
    assert entry.total_debit == Decimal("100.00")
    assert entry.total_credit == Decimal("100.00")
    assert entry.is_balanced is True
```

- [ ] **Step 3: Run it to confirm it fails**

Run: `.venv/bin/python -m pytest apps/payments/tests/test_ledger.py::test_journal_entry_balances -q`
Expected: FAIL — `ImportError` / `cannot import name 'JournalEntry'`.

- [ ] **Step 4: Add the models** — append to `apps/payments/models.py` (the file already imports `Decimal`, `settings`, `models`, `MoneyField`, `TimeStampedModel`):

```python
from apps.core.choices import Account  # add to the existing imports at top


class JournalEntry(TimeStampedModel):
    """One balanced, immutable accounting event (debits == credits)."""

    class Kind(models.TextChoices):
        DEPOSIT_CAPTURED = "deposit_captured", "Deposit captured"
        BALANCE_CAPTURED = "balance_captured", "Balance captured"
        REVENUE_RECOGNIZED = "revenue_recognized", "Revenue recognized"
        REFUND_ISSUED = "refund_issued", "Refund issued"
        DEPOSIT_FORFEITED = "deposit_forfeited", "Deposit forfeited"
        REVERSAL = "reversal", "Reversal"
        ADJUSTMENT = "adjustment", "Adjustment"

    class Source(models.TextChoices):
        STRIPE = "stripe", "Stripe"
        SYSTEM = "system", "System"
        MANUAL = "manual", "Manual"

    lead = models.ForeignKey(
        "leads.Lead", related_name="journal_entries", on_delete=models.PROTECT
    )
    reservation = models.ForeignKey(
        "reservations.Reservation",
        related_name="journal_entries",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    kind = models.CharField(max_length=32, choices=Kind.choices)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.SYSTEM)
    memo = models.CharField(max_length=255, blank=True)
    charge = models.ForeignKey(
        "payments.Charge", null=True, blank=True, on_delete=models.SET_NULL
    )
    stripe_ref = models.CharField(max_length=64, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    idempotency_key = models.CharField(max_length=120, unique=True)
    posted_at = models.DateTimeField(auto_now_add=True)

    @property
    def total_debit(self) -> Decimal:
        return sum((line.debit for line in self.lines.all()), Decimal("0.00"))

    @property
    def total_credit(self) -> Decimal:
        return sum((line.credit for line in self.lines.all()), Decimal("0.00"))

    @property
    def is_balanced(self) -> bool:
        return self.total_debit == self.total_credit

    def __str__(self) -> str:
        return f"{self.get_kind_display()} · {self.lead.quote_no}"


class JournalLine(TimeStampedModel):
    """One debit-or-credit posting against an account, inside a JournalEntry."""

    entry = models.ForeignKey(JournalEntry, related_name="lines", on_delete=models.CASCADE)
    account = models.CharField(max_length=32, choices=Account.choices)
    debit = MoneyField()
    credit = MoneyField()

    def __str__(self) -> str:
        return f"{self.get_account_display()} D{self.debit}/C{self.credit}"
```

- [ ] **Step 5: Make and run the migration**

Run: `.venv/bin/python manage.py makemigrations payments && .venv/bin/python manage.py migrate`
Expected: a new `payments/migrations/000X_journalentry_journalline.py`; migrate applies cleanly.

- [ ] **Step 6: Run the test to confirm it passes**

Run: `.venv/bin/python -m pytest apps/payments/tests/test_ledger.py::test_journal_entry_balances -q`
Expected: PASS.

- [ ] **Step 7: Register admin** — add to `apps/payments/admin.py`:

```python
from .models import Charge, JournalEntry, JournalLine, PaymentPlan  # extend existing import


class JournalLineInline(admin.TabularInline):
    model = JournalLine
    extra = 0
    readonly_fields = ("created_at",)


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = ("kind", "lead", "reservation", "source", "memo", "posted_at")
    list_filter = ("kind", "source")
    search_fields = ("idempotency_key", "lead__contact__name", "memo")
    inlines = [JournalLineInline]
```

- [ ] **Step 8: Commit**

```bash
git add apps/core/choices.py apps/payments/models.py apps/payments/admin.py apps/payments/migrations apps/payments/tests/test_ledger.py
git commit -m "Add double-entry ledger models (JournalEntry/JournalLine) + Account enum"
```

---

## Task 2: Posting core — `post_entry`, `account_balance`, `order_balances`

**Files:**
- Create: `apps/payments/ledger.py`
- Test: `apps/payments/tests/test_ledger.py` (extend)

- [ ] **Step 1: Write the failing tests** — append to `apps/payments/tests/test_ledger.py`:

```python
from apps.payments import ledger


def test_post_entry_creates_balanced_entry():
    lead = LeadFactory()
    entry = ledger.post_entry(
        lead=lead,
        kind=JournalEntry.Kind.ADJUSTMENT,
        lines=[
            (Account.CASH, Decimal("50.00"), Decimal("0.00")),
            (Account.CUSTOMER_DEPOSITS, Decimal("0.00"), Decimal("50.00")),
        ],
        idempotency_key="ok1",
    )
    assert entry.is_balanced
    assert entry.lines.count() == 2


def test_post_entry_rejects_unbalanced():
    lead = LeadFactory()
    with pytest.raises(ledger.LedgerError):
        ledger.post_entry(
            lead=lead,
            kind=JournalEntry.Kind.ADJUSTMENT,
            lines=[(Account.CASH, Decimal("10.00"), Decimal("0.00"))],
            idempotency_key="bad1",
        )


def test_post_entry_is_idempotent():
    lead = LeadFactory()
    lines = [
        (Account.CASH, Decimal("10.00"), Decimal("0.00")),
        (Account.CUSTOMER_DEPOSITS, Decimal("0.00"), Decimal("10.00")),
    ]
    first = ledger.post_entry(
        lead=lead, kind=JournalEntry.Kind.ADJUSTMENT, lines=lines, idempotency_key="dup"
    )
    second = ledger.post_entry(
        lead=lead, kind=JournalEntry.Kind.ADJUSTMENT, lines=lines, idempotency_key="dup"
    )
    assert first.pk == second.pk
    assert JournalEntry.objects.filter(idempotency_key="dup").count() == 1
```

- [ ] **Step 2: Run to confirm failure**

Run: `.venv/bin/python -m pytest apps/payments/tests/test_ledger.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'apps.payments.ledger'`.

- [ ] **Step 3: Create `apps/payments/ledger.py`**

```python
"""Double-entry posting service — the only writer of the ledger (spec §3–§4)."""

from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from apps.core.choices import Account

from .models import JournalEntry, JournalLine

ZERO = Decimal("0.00")


class LedgerError(Exception):
    """Raised when an entry would be unbalanced."""


def post_entry(
    *,
    lead,
    kind,
    lines,
    idempotency_key,
    reservation=None,
    charge=None,
    source=JournalEntry.Source.SYSTEM,
    memo="",
    created_by=None,
    stripe_ref="",
):
    """Post one balanced entry. `lines` = list of (account, debit, credit). Idempotent."""
    existing = JournalEntry.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        return existing

    total_debit = sum((debit for _, debit, _ in lines), ZERO)
    total_credit = sum((credit for _, _, credit in lines), ZERO)
    if total_debit != total_credit:
        raise LedgerError(
            f"Unbalanced entry {idempotency_key!r}: debit {total_debit} != credit {total_credit}"
        )

    with transaction.atomic():
        entry = JournalEntry.objects.create(
            lead=lead,
            reservation=reservation,
            kind=kind,
            source=source,
            memo=memo,
            charge=charge,
            created_by=created_by,
            stripe_ref=stripe_ref,
            idempotency_key=idempotency_key,
        )
        JournalLine.objects.bulk_create(
            [
                JournalLine(entry=entry, account=account, debit=debit, credit=credit)
                for account, debit, credit in lines
            ]
        )
    return entry


def account_balance(lead, account) -> Decimal:
    """Σ debit − Σ credit for one account on one order."""
    agg = JournalLine.objects.filter(entry__lead=lead, account=account).aggregate(
        d=Sum("debit"), c=Sum("credit")
    )
    return (agg["d"] or ZERO) - (agg["c"] or ZERO)


def order_balances(lead) -> dict:
    """Business-meaningful positive balances for an order."""
    return {
        "collected": account_balance(lead, Account.CASH),
        "deferred": -account_balance(lead, Account.CUSTOMER_DEPOSITS),
        "ar": account_balance(lead, Account.ACCOUNTS_RECEIVABLE),
        "recognized": -account_balance(lead, Account.RECOGNIZED_REVENUE),
        "refunded": account_balance(lead, Account.REFUNDS),
    }
```

- [ ] **Step 4: Run to confirm pass**

Run: `.venv/bin/python -m pytest apps/payments/tests/test_ledger.py -q`
Expected: PASS (all ledger tests).

- [ ] **Step 5: Commit**

```bash
git add apps/payments/ledger.py apps/payments/tests/test_ledger.py
git commit -m "Add ledger posting service (post_entry, balances) with balance + idempotency guards"
```

---

## Task 3: `post_capture` — deposit/balance cash in (clears A/R first)

**Files:**
- Modify: `apps/payments/ledger.py`
- Test: `apps/payments/tests/test_ledger.py` (extend)

- [ ] **Step 1: Write the failing tests** — append to `apps/payments/tests/test_ledger.py`:

```python
def test_capture_credits_deferred():
    lead = LeadFactory()
    ledger.post_capture(
        lead=lead,
        amount=Decimal("1335.00"),
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED,
        idempotency_key="cap-dep",
    )
    bals = ledger.order_balances(lead)
    assert bals["collected"] == Decimal("1335.00")
    assert bals["deferred"] == Decimal("1335.00")
    assert bals["ar"] == Decimal("0.00")


def test_capture_clears_ar_before_deferred():
    lead = LeadFactory()
    # Manually create $165 of A/R (Dr A/R / Cr Recognized Revenue).
    ledger.post_entry(
        lead=lead,
        kind=JournalEntry.Kind.ADJUSTMENT,
        lines=[
            (Account.ACCOUNTS_RECEIVABLE, Decimal("165.00"), Decimal("0.00")),
            (Account.RECOGNIZED_REVENUE, Decimal("0.00"), Decimal("165.00")),
        ],
        idempotency_key="seed-ar",
    )
    ledger.post_capture(
        lead=lead,
        amount=Decimal("1335.00"),
        kind=JournalEntry.Kind.BALANCE_CAPTURED,
        idempotency_key="cap-bal",
    )
    bals = ledger.order_balances(lead)
    assert bals["ar"] == Decimal("0.00")
    assert bals["deferred"] == Decimal("1170.00")  # 1335 − 165 cleared from A/R
```

- [ ] **Step 2: Run to confirm failure**

Run: `.venv/bin/python -m pytest apps/payments/tests/test_ledger.py -k capture -q`
Expected: FAIL — `AttributeError: module 'apps.payments.ledger' has no attribute 'post_capture'`.

- [ ] **Step 3: Add `post_capture` to `apps/payments/ledger.py`**

```python
def post_capture(*, lead, amount, kind, idempotency_key, charge=None,
                 source=JournalEntry.Source.STRIPE, memo=""):
    """Cash in. Clears any outstanding A/R first, then adds to deferred revenue."""
    amount = Decimal(amount)
    to_ar = min(amount, order_balances(lead)["ar"])
    to_deferred = amount - to_ar
    lines = [(Account.CASH, amount, ZERO)]
    if to_ar > ZERO:
        lines.append((Account.ACCOUNTS_RECEIVABLE, ZERO, to_ar))
    if to_deferred > ZERO:
        lines.append((Account.CUSTOMER_DEPOSITS, ZERO, to_deferred))
    return post_entry(
        lead=lead, kind=kind, lines=lines, idempotency_key=idempotency_key,
        charge=charge, source=source, memo=memo,
    )
```

- [ ] **Step 4: Run to confirm pass**

Run: `.venv/bin/python -m pytest apps/payments/tests/test_ledger.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/payments/ledger.py apps/payments/tests/test_ledger.py
git commit -m "Add post_capture (cash in, clears A/R before deferred)"
```

---

## Task 4: `Reservation` revenue fields + `EARNED_TERMINAL_STATUSES`

**Files:**
- Modify: `apps/reservations/models.py`
- Test: `apps/reservations/tests/test_models.py` (extend)

- [ ] **Step 1: Write the failing test** — append to `apps/reservations/tests/test_models.py`:

```python
from apps.reservations.models import EARNED_TERMINAL_STATUSES, Reservation


def test_reservation_defaults_to_deferred_revenue(db):
    from apps.reservations.factories import TransferReservationFactory

    res = TransferReservationFactory()
    assert res.revenue_status == Reservation.RevenueStatus.DEFERRED
    assert res.recognized_at is None


def test_earned_terminal_statuses():
    assert Reservation.TripStatus.DONE in EARNED_TERMINAL_STATUSES
    assert Reservation.TripStatus.NO_SHOW in EARNED_TERMINAL_STATUSES
    assert Reservation.TripStatus.CANCELLED not in EARNED_TERMINAL_STATUSES
```

- [ ] **Step 2: Run to confirm failure**

Run: `.venv/bin/python -m pytest apps/reservations/tests/test_models.py -k "deferred or earned_terminal" -q`
Expected: FAIL — `ImportError: cannot import name 'EARNED_TERMINAL_STATUSES'`.

- [ ] **Step 3: Add the fields + constant to `apps/reservations/models.py`**

Add the `RevenueStatus` class and three fields inside `Reservation` (next to the other fields, before `class Meta`):

```python
    class RevenueStatus(models.TextChoices):
        DEFERRED = "deferred", "Deferred"
        RECOGNIZED = "recognized", "Recognized"
        REVERSED = "reversed", "Reversed"

    revenue_status = models.CharField(
        max_length=20, choices=RevenueStatus.choices, default=RevenueStatus.DEFERRED
    )
    recognized_at = models.DateTimeField(null=True, blank=True)
    recognized_amount = MoneyField()
```

Add at the very end of the module (after the `TripStatusEvent` class), so it can reference `Reservation`:

```python
# Trip statuses that count as earned revenue (the vehicle was provided), per spec §5.1.
EARNED_TERMINAL_STATUSES = (
    Reservation.TripStatus.DONE,
    Reservation.TripStatus.NO_SHOW,
)
```

- [ ] **Step 4: Make and run the migration**

Run: `.venv/bin/python manage.py makemigrations reservations && .venv/bin/python manage.py migrate`
Expected: new `reservations/migrations/000X_reservation_revenue_status_*.py`; applies cleanly.

- [ ] **Step 5: Run the tests to confirm they pass**

Run: `.venv/bin/python -m pytest apps/reservations/tests/test_models.py -k "deferred or earned_terminal" -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/reservations/models.py apps/reservations/migrations apps/reservations/tests/test_models.py
git commit -m "Add Reservation revenue fields + EARNED_TERMINAL_STATUSES"
```

---

## Task 5: `recognize_reservation` — per-trip recognition (deferred → revenue, overflow to A/R)

**Files:**
- Modify: `apps/payments/ledger.py`
- Test: `apps/payments/tests/test_recognition.py` (create)

- [ ] **Step 1: Write the failing tests** — create `apps/payments/tests/test_recognition.py`:

```python
from decimal import Decimal

import pytest

from apps.leads.factories import LeadFactory
from apps.payments import ledger
from apps.payments.models import JournalEntry
from apps.reservations.factories import TransferReservationFactory
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db


def test_recognition_draws_from_deferred():
    lead = LeadFactory()
    res = TransferReservationFactory(lead=lead, base_rate=Decimal("1500.00"))
    ledger.post_capture(
        lead=lead, amount=Decimal("2670.00"),
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED, idempotency_key="cap1",
    )
    ledger.recognize_reservation(res)
    bals = ledger.order_balances(lead)
    assert bals["recognized"] == Decimal("1500.00")
    assert bals["deferred"] == Decimal("1170.00")
    assert bals["ar"] == Decimal("0.00")
    res.refresh_from_db()
    assert res.revenue_status == Reservation.RevenueStatus.RECOGNIZED
    assert res.recognized_amount == Decimal("1500.00")
    assert res.recognized_at is not None


def test_recognition_overflows_to_ar_when_underpaid():
    lead = LeadFactory()
    res = TransferReservationFactory(lead=lead, base_rate=Decimal("1500.00"))
    ledger.post_capture(
        lead=lead, amount=Decimal("1335.00"),
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED, idempotency_key="cap1",
    )
    ledger.recognize_reservation(res)
    bals = ledger.order_balances(lead)
    assert bals["recognized"] == Decimal("1500.00")
    assert bals["deferred"] == Decimal("0.00")
    assert bals["ar"] == Decimal("165.00")


def test_recognition_is_idempotent():
    lead = LeadFactory()
    res = TransferReservationFactory(lead=lead, base_rate=Decimal("1500.00"))
    ledger.post_capture(
        lead=lead, amount=Decimal("1500.00"),
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED, idempotency_key="cap1",
    )
    ledger.recognize_reservation(res)
    ledger.recognize_reservation(res)
    assert (
        JournalEntry.objects.filter(
            reservation=res, kind=JournalEntry.Kind.REVENUE_RECOGNIZED
        ).count()
        == 1
    )
```

- [ ] **Step 2: Run to confirm failure**

Run: `.venv/bin/python -m pytest apps/payments/tests/test_recognition.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'recognize_reservation'`.

- [ ] **Step 3: Add `recognize_reservation` to `apps/payments/ledger.py`**

Add the import at the top of the module:

```python
from django.utils import timezone
```

Then add the function:

```python
def recognize_reservation(reservation):
    """Recognize one trip's revenue: draw deferred first, overflow to A/R. Idempotent."""
    from apps.reservations.models import Reservation

    lead = reservation.lead
    amount = Decimal(reservation.line_total)
    deferred = order_balances(lead)["deferred"]
    from_deferred = min(amount, deferred) if deferred > ZERO else ZERO
    to_ar = amount - from_deferred

    lines = [(Account.RECOGNIZED_REVENUE, ZERO, amount)]
    if from_deferred > ZERO:
        lines.append((Account.CUSTOMER_DEPOSITS, from_deferred, ZERO))
    if to_ar > ZERO:
        lines.append((Account.ACCOUNTS_RECEIVABLE, to_ar, ZERO))

    entry = post_entry(
        lead=lead,
        reservation=reservation,
        kind=JournalEntry.Kind.REVENUE_RECOGNIZED,
        lines=lines,
        idempotency_key=f"recognize-res{reservation.pk}",
        memo=f"Recognize {reservation}",
    )
    if reservation.revenue_status != Reservation.RevenueStatus.RECOGNIZED:
        reservation.revenue_status = Reservation.RevenueStatus.RECOGNIZED
        reservation.recognized_at = timezone.now()
        reservation.recognized_amount = amount
        reservation.save(
            update_fields=["revenue_status", "recognized_at", "recognized_amount", "updated_at"]
        )
    return entry
```

- [ ] **Step 4: Run to confirm pass**

Run: `.venv/bin/python -m pytest apps/payments/tests/test_recognition.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/payments/ledger.py apps/payments/tests/test_recognition.py
git commit -m "Add per-trip revenue recognition (deferred -> revenue, overflow to A/R)"
```

---

## Task 6: `recognize_due_revenue` nightly task

**Files:**
- Modify: `apps/payments/tasks.py`
- Modify: `config/settings/base.py`
- Test: `apps/payments/tests/test_recognition.py` (extend)

- [ ] **Step 1: Write the failing test** — append to `apps/payments/tests/test_recognition.py`:

```python
from datetime import date, timedelta

from apps.payments.tasks import recognize_due_revenue


def test_recognize_due_revenue_only_earned_past_trips():
    lead = LeadFactory()
    ledger.post_capture(
        lead=lead, amount=Decimal("3000.00"),
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED, idempotency_key="cap1",
    )
    past = date.today() - timedelta(days=1)
    future = date.today() + timedelta(days=10)
    done_past = TransferReservationFactory(
        lead=lead, base_rate=Decimal("1000.00"),
        pickup_date=past, trip_status=Reservation.TripStatus.DONE,
    )
    noshow_past = TransferReservationFactory(
        lead=lead, base_rate=Decimal("500.00"),
        pickup_date=past, trip_status=Reservation.TripStatus.NO_SHOW,
    )
    done_future = TransferReservationFactory(
        lead=lead, base_rate=Decimal("700.00"),
        pickup_date=future, trip_status=Reservation.TripStatus.DONE,
    )
    cancelled_past = TransferReservationFactory(
        lead=lead, base_rate=Decimal("400.00"),
        pickup_date=past, trip_status=Reservation.TripStatus.CANCELLED,
    )

    assert recognize_due_revenue() == 2  # done_past + noshow_past only

    for res in (done_past, noshow_past):
        res.refresh_from_db()
        assert res.revenue_status == Reservation.RevenueStatus.RECOGNIZED
    for res in (done_future, cancelled_past):
        res.refresh_from_db()
        assert res.revenue_status == Reservation.RevenueStatus.DEFERRED

    assert recognize_due_revenue() == 0  # idempotent second run
```

- [ ] **Step 2: Run to confirm failure**

Run: `.venv/bin/python -m pytest apps/payments/tests/test_recognition.py -k due_revenue -q`
Expected: FAIL — `ImportError: cannot import name 'recognize_due_revenue'`.

- [ ] **Step 3: Add the task** — append to `apps/payments/tasks.py` (extend imports + add task):

```python
from django.utils import timezone

from . import ledger
from .models import PaymentPlan  # already imported; keep one import line
from apps.reservations.models import EARNED_TERMINAL_STATUSES, Reservation


@shared_task
def recognize_due_revenue() -> int:
    """Recognize each earned, past-pickup, still-deferred trip's revenue (spec §5)."""
    today = timezone.localdate()
    due = Reservation.objects.filter(
        pickup_date__lt=today,
        trip_status__in=EARNED_TERMINAL_STATUSES,
        revenue_status=Reservation.RevenueStatus.DEFERRED,
    ).select_related("lead")
    count = 0
    for reservation in due:
        ledger.recognize_reservation(reservation)
        count += 1
    return count
```

- [ ] **Step 4: Run to confirm pass**

Run: `.venv/bin/python -m pytest apps/payments/tests/test_recognition.py -q`
Expected: PASS.

- [ ] **Step 5: Add the beat schedule** — in `config/settings/base.py`, add to the existing `CELERY_BEAT_SCHEDULE` dict (after the `charge-due-balances` entry):

```python
    "recognize-due-revenue": {
        "task": "apps.payments.tasks.recognize_due_revenue",
        "schedule": crontab(hour=2, minute=0),  # nightly — recognize completed trips
    },
```

- [ ] **Step 6: Verify the project still checks out**

Run: `.venv/bin/python manage.py check`
Expected: "System check identified no issues".

- [ ] **Step 7: Commit**

```bash
git add apps/payments/tasks.py config/settings/base.py apps/payments/tests/test_recognition.py
git commit -m "Add nightly recognize_due_revenue task + beat schedule"
```

---

## Task 7: Wire Stripe deposit + balance capture into the ledger

**Files:**
- Modify: `apps/payments/webhooks.py` (deposit)
- Modify: `apps/payments/services.py` (balance)
- Test: `apps/payments/tests/test_ledger_wiring.py` (create)

- [ ] **Step 1: Write the failing tests** — create `apps/payments/tests/test_ledger_wiring.py`:

```python
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from apps.payments import ledger, services, webhooks
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import PaymentPlan

pytestmark = pytest.mark.django_db


def _session_event(lead_id):
    return {
        "type": "checkout.session.completed",
        "data": {"object": {"metadata": {"lead_id": str(lead_id)}, "payment_intent": "pi_1"}},
    }


def _saved_pm():
    return MagicMock(
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242"))
    )


def test_deposit_webhook_posts_capture():
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    with patch.object(webhooks.stripe.PaymentIntent, "retrieve", return_value=_saved_pm()):
        webhooks.process_stripe_event(_session_event(plan.lead_id))
    bals = ledger.order_balances(plan.lead)
    assert bals["collected"] == Decimal("1335.00")
    assert bals["deferred"] == Decimal("1335.00")


def test_balance_charge_posts_capture():
    plan = PaymentPlanFactory(
        quote_total=Decimal("2670.00"),
        stripe_customer_id="cus_1",
        stripe_payment_method_id="pm_1",
        balance_status=PaymentPlan.BalanceStatus.SCHEDULED,
    )
    with patch.object(
        services.stripe.PaymentIntent, "create", return_value=MagicMock(id="pi_bal")
    ):
        services.charge_balance(plan)
    bals = ledger.order_balances(plan.lead)
    assert bals["collected"] == Decimal("1335.00")
    assert bals["deferred"] == Decimal("1335.00")
```

- [ ] **Step 2: Run to confirm failure**

Run: `.venv/bin/python -m pytest apps/payments/tests/test_ledger_wiring.py -q`
Expected: FAIL — balances are `0.00` (no capture posted yet).

- [ ] **Step 3: Post a deposit capture in `apps/payments/webhooks.py`**

Add `from . import ledger` to the imports. Then replace the "Record the deposit as captured." block in `_deposit_completed` with:

```python
    # Record the deposit Charge as captured and post the ledger entry.
    charge = plan.charges.filter(kind=Charge.Kind.DEPOSIT).first()
    if charge is None:
        charge = plan.record_charge(kind=Charge.Kind.DEPOSIT, amount=plan.deposit_amount)
    if charge.status != Charge.Status.SUCCEEDED:
        charge.status = Charge.Status.SUCCEEDED
        charge.save(update_fields=["status", "updated_at"])
    ledger.post_capture(
        lead=plan.lead,
        amount=plan.deposit_amount,
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED,
        idempotency_key=f"capture-charge{charge.pk}",
        charge=charge,
        memo="Deposit captured",
    )
```

Add `JournalEntry` to the model import in `webhooks.py`: `from .models import Charge, JournalEntry, PaymentPlan`.

- [ ] **Step 4: Post a balance capture in `apps/payments/services.py`**

Add `from . import ledger` and `JournalEntry` to imports. In `charge_balance`, after the success block sets `plan.balance_status = PAID` and saves, before `return charge`, add:

```python
    ledger.post_capture(
        lead=plan.lead,
        amount=plan.balance_amount,
        kind=JournalEntry.Kind.BALANCE_CAPTURED,
        idempotency_key=f"capture-charge{charge.pk}",
        charge=charge,
        memo="Balance captured",
    )
```

(`charge` is the `Charge` already created at the top of `charge_balance` via `plan.record_charge(...)`.)

- [ ] **Step 5: Run the wiring tests + the existing payments suite**

Run: `.venv/bin/python -m pytest apps/payments -q`
Expected: PASS (new wiring tests + existing `test_services.py` / `test_stripe_webhook.py` still green).

- [ ] **Step 6: Commit**

```bash
git add apps/payments/webhooks.py apps/payments/services.py apps/payments/tests/test_ledger_wiring.py
git commit -m "Post ledger capture entries on Stripe deposit + balance success"
```

---

## Task 8: Refund + forfeit posting helpers (service layer for the Phase-2 review surface)

**Files:**
- Modify: `apps/payments/ledger.py`
- Test: `apps/payments/tests/test_ledger.py` (extend)

- [ ] **Step 1: Write the failing tests** — append to `apps/payments/tests/test_ledger.py`:

```python
def test_refund_reverses_deferred_then_revenue():
    lead = LeadFactory()
    ledger.post_capture(
        lead=lead, amount=Decimal("1000.00"),
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED, idempotency_key="cap1",
    )
    ledger.post_refund(lead=lead, amount=Decimal("400.00"), idempotency_key="ref1")
    bals = ledger.order_balances(lead)
    assert bals["deferred"] == Decimal("600.00")
    assert bals["collected"] == Decimal("600.00")  # cash credited back
    assert bals["refunded"] == Decimal("0.00")  # all came from deferred


def test_forfeit_moves_deferred_to_cancellation_revenue():
    lead = LeadFactory()
    ledger.post_capture(
        lead=lead, amount=Decimal("500.00"),
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED, idempotency_key="cap1",
    )
    entry = ledger.post_forfeit(lead=lead, amount=Decimal("500.00"), idempotency_key="forf1")
    assert entry.is_balanced
    bals = ledger.order_balances(lead)
    assert bals["deferred"] == Decimal("0.00")
    from apps.core.choices import Account as _A

    assert -ledger.account_balance(lead, _A.CANCELLATION_REVENUE) == Decimal("500.00")
```

- [ ] **Step 2: Run to confirm failure**

Run: `.venv/bin/python -m pytest apps/payments/tests/test_ledger.py -k "refund or forfeit" -q`
Expected: FAIL — `AttributeError: ... has no attribute 'post_refund'`.

- [ ] **Step 3: Add `post_refund` + `post_forfeit` to `apps/payments/ledger.py`**

```python
def post_refund(*, lead, amount, idempotency_key, charge=None, created_by=None, memo="Refund"):
    """Cash out. Reverses deferred first; any excess reverses recognized revenue (Refunds)."""
    amount = Decimal(amount)
    deferred = order_balances(lead)["deferred"]
    from_deferred = min(amount, deferred) if deferred > ZERO else ZERO
    from_refunds = amount - from_deferred
    lines = [(Account.CASH, ZERO, amount)]
    if from_deferred > ZERO:
        lines.append((Account.CUSTOMER_DEPOSITS, from_deferred, ZERO))
    if from_refunds > ZERO:
        lines.append((Account.REFUNDS, from_refunds, ZERO))
    return post_entry(
        lead=lead, kind=JournalEntry.Kind.REFUND_ISSUED, lines=lines,
        idempotency_key=idempotency_key, charge=charge, created_by=created_by,
        source=JournalEntry.Source.MANUAL, memo=memo,
    )


def post_forfeit(*, lead, amount, idempotency_key, created_by=None, memo="Deposit forfeited"):
    """Cancellation: reclassify deferred cash to cancellation revenue. No cash moves."""
    amount = Decimal(amount)
    lines = [
        (Account.CUSTOMER_DEPOSITS, amount, ZERO),
        (Account.CANCELLATION_REVENUE, ZERO, amount),
    ]
    return post_entry(
        lead=lead, kind=JournalEntry.Kind.DEPOSIT_FORFEITED, lines=lines,
        idempotency_key=idempotency_key, created_by=created_by,
        source=JournalEntry.Source.MANUAL, memo=memo,
    )
```

- [ ] **Step 4: Run to confirm pass**

Run: `.venv/bin/python -m pytest apps/payments/tests/test_ledger.py -q`
Expected: PASS.

- [ ] **Step 5: Full suite + lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check apps/payments apps/reservations apps/core config && .venv/bin/ruff format apps/payments apps/reservations`
Expected: all tests pass; ruff clean (format makes no changes, or re-add formatted files).

- [ ] **Step 6: Commit**

```bash
git add apps/payments/ledger.py apps/payments/tests/test_ledger.py
git commit -m "Add post_refund + post_forfeit ledger helpers for the review surface"
```

---

## Self-review (spec coverage)

- **§3 chart of accounts / double-entry / immutability / idempotency** → Tasks 1–2 (`Account`, models, `post_entry` balance + idempotency guards).
- **§4 posting rules** → capture (Task 3), recognition (Task 5), refund + forfeit (Task 8); the failed-balance→A/R case is `test_recognition_overflows_to_ar_when_underpaid`; recovery is `test_capture_clears_ar_before_deferred`.
- **§5 per-trip nightly recognition (`done` + `no_show`, exclude cancelled/future)** → Tasks 4 + 6.
- **§3.3 derived balances** → `order_balances` (Task 2).
- Webhook capture wiring (deposits + balance) → Task 7.
- **Deferred to follow-on plans (stated up front):** itemized `ReservationCharge` refactor; vendor A/P (`VendorBill`/`VendorCharge`/`VendorPayment`); Orders review surface (§8); payment touchpoints (§9); Stripe fee capture (§13).

---

## Execution Handoff

Plan complete and saved to `docs/plans/2026-06-14-payments-accounting-core.md`. Two execution options:

1. **Subagent-Driven (recommended)** — a fresh subagent per task, with review between tasks.
2. **Inline Execution** — execute tasks in this session with checkpoints for review.

Which approach?
