from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models

from apps.core.choices import Account
from apps.core.fields import MoneyField
from apps.core.models import TimeStampedModel


def default_deposit_pct() -> int:
    """Runtime default so the deposit policy follows STRIPE_DEPOSIT_PCT."""
    return settings.STRIPE_DEPOSIT_PCT


class PaymentPlan(TimeStampedModel):
    """Deposit + balance plan for one quote (Stripe). One per Lead."""

    class DepositStatus(models.TextChoices):
        UNSENT = "unsent", "Unsent"
        REQUESTED = "requested", "Requested"
        # Card authorized, money on hold, nothing captured — waiting on APC to confirm
        # the order (APC-26).
        AUTHORIZED = "authorized", "Authorized"
        PAID = "paid", "Paid"

    class BalanceStatus(models.TextChoices):
        NA = "na", "N/A"
        SCHEDULED = "scheduled", "Scheduled"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"

    lead = models.OneToOneField("leads.Lead", related_name="payment", on_delete=models.CASCADE)
    deposit_pct = models.PositiveSmallIntegerField(default=default_deposit_pct)
    quote_total = MoneyField()  # snapshot taken when the quote/deposit is sent

    deposit_status = models.CharField(
        max_length=20, choices=DepositStatus.choices, default=DepositStatus.UNSENT
    )
    balance_status = models.CharField(
        max_length=20, choices=BalanceStatus.choices, default=BalanceStatus.NA
    )

    processor = models.CharField(max_length=20, default="stripe")
    stripe_customer_id = models.CharField(max_length=64, blank=True)
    stripe_payment_method_id = models.CharField(max_length=64, blank=True)
    card_brand = models.CharField(max_length=20, blank=True)
    card_last4 = models.CharField(max_length=4, blank=True)
    fail_reason = models.CharField(max_length=255, blank=True)

    # --- amounts ---
    @property
    def deposit_amount(self) -> Decimal:
        return (Decimal(self.quote_total) * Decimal(self.deposit_pct) / Decimal(100)).quantize(
            Decimal("0.01")
        )

    @property
    def balance_amount(self) -> Decimal:
        return (Decimal(self.quote_total) - self.deposit_amount).quantize(Decimal("0.01"))

    def snapshot_total(self, *, save: bool = True) -> None:
        """Freeze the quote total from the lead (so figures don't drift after send)."""
        self.quote_total = self.lead.quote_total
        if save:
            self.save(update_fields=["quote_total", "updated_at"])

    # --- balance schedule ---
    @property
    def earliest_pickup(self):
        dates = [r.pickup_date for r in self.lead.reservations.all() if r.pickup_date]
        return min(dates) if dates else None

    @property
    def balance_due_date(self):
        pickup = self.earliest_pickup
        if pickup is None:
            return None
        return pickup - timedelta(days=settings.BALANCE_CHARGE_DAYS_BEFORE)

    @property
    def balance_due_now(self) -> bool:
        due = self.balance_due_date
        return due is not None and due <= date.today()

    @property
    def is_paid_in_full(self) -> bool:
        return (
            self.deposit_status == self.DepositStatus.PAID
            and self.balance_status == self.BalanceStatus.PAID
        )

    def record_charge(self, *, kind: str, amount: Decimal, **extra) -> "Charge":
        """Create the next Charge attempt for this plan with a derived idempotency key.
        `extra` carries kind-specific fields (a final bill's overtime split)."""
        attempt = self.charges.filter(kind=kind).count() + 1
        return self.charges.create(
            kind=kind,
            amount=amount,
            attempt_no=attempt,
            idempotency_key=f"plan{self.pk}-{kind}-{attempt}",
            **extra,
        )

    def __str__(self) -> str:
        return f"Payment plan · {self.lead.quote_no}"


class Charge(TimeStampedModel):
    """A single Stripe charge attempt (deposit or balance) — idempotent + auditable."""

    class Kind(models.TextChoices):
        DEPOSIT = "deposit", "Deposit"
        BALANCE = "balance", "Balance"
        REFUND = "refund", "Refund"
        # The post-trip bill (APC-60): approved overtime + whatever is left of the order
        # balance, one per order once every trip is reviewed.
        FINAL = "final", "Final bill"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        # Manual-capture deposit states (APC-26): the hold exists (AUTHORIZED), was taken
        # (SUCCEEDED), was let go by us (RELEASED), or lapsed on the issuer's clock
        # (EXPIRED). Only SUCCEEDED ever moves money.
        AUTHORIZED = "authorized", "Authorized"
        SUCCEEDED = "succeeded", "Succeeded"
        RELEASED = "released", "Released"
        EXPIRED = "expired", "Expired"
        FAILED = "failed", "Failed"

    plan = models.ForeignKey(PaymentPlan, related_name="charges", on_delete=models.CASCADE)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    amount = MoneyField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    stripe_payment_intent_id = models.CharField(max_length=64, blank=True)
    # Stored so reusing an open intent stays a local lookup — see services.open_intent_for.
    # Not a standalone credential: it confirms this one intent, and the token-holder it is
    # handed to is already authorised to pay this charge.
    stripe_client_secret = models.CharField(max_length=255, blank=True)
    stripe_refund_id = models.CharField(max_length=64, blank=True)
    # A FINAL charge's overtime part (base + gratuity) and the gratuity within it, so the
    # ledger can split it: amount − overtime posts as a balance payment, the rest as
    # revenue (`services._post_final`). 0 on every other kind.
    overtime_amount = MoneyField()
    overtime_gratuity = MoneyField()
    # The card that actually paid *this* charge, snapshotted when the money moved or was
    # held. `PaymentPlan.card_brand` is only ever the card currently on file, and
    # `save_payment_method` overwrites it — so without this, a customer swapping cards
    # leaves nothing able to say what paid the deposit but Stripe. Brand and last four
    # only; never a PAN, an expiry or a CVC. Blank on every row that predates APC-40.
    card_brand = models.CharField(max_length=20, blank=True)
    card_last4 = models.CharField(max_length=4, blank=True)
    idempotency_key = models.CharField(max_length=120, unique=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    attempt_no = models.PositiveSmallIntegerField(default=1)
    attempted_at = models.DateTimeField(null=True, blank=True)
    # Manual-capture timeline (APC-26). `capture_expires_at` is our *estimate* of the
    # issuer's hold window — the card networks decide, and debit is often shorter than
    # credit, so it drives the countdown and the alerts while Stripe's own state decides
    # whether a capture actually succeeds.
    authorized_at = models.DateTimeField(null=True, blank=True)
    capture_expires_at = models.DateTimeField(null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    @property
    def succeeded(self) -> bool:
        return self.status == self.Status.SUCCEEDED

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.amount} · {self.get_status_display()}"


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
        OVERTIME_CAPTURED = "overtime_captured", "Overtime captured"
        PAYABLE_ACCRUED = "payable_accrued", "Affiliate payable accrued"
        PAYABLE_PAID = "payable_paid", "Affiliate payable paid"

    class Source(models.TextChoices):
        STRIPE = "stripe", "Stripe"
        SYSTEM = "system", "System"
        MANUAL = "manual", "Manual"

    lead = models.ForeignKey("leads.Lead", related_name="journal_entries", on_delete=models.PROTECT)
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
    charge = models.ForeignKey("payments.Charge", null=True, blank=True, on_delete=models.SET_NULL)
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
        agg = self.lines.aggregate(d=models.Sum("debit"), c=models.Sum("credit"))
        if agg["d"] is None:  # no lines yet
            return False
        return agg["d"] == (agg["c"] or Decimal("0.00"))

    def __str__(self) -> str:
        return f"{self.get_kind_display()} · {self.lead.quote_no}"


class JournalLine(TimeStampedModel):
    """One debit-or-credit posting against an account, inside a JournalEntry."""

    entry = models.ForeignKey(JournalEntry, related_name="lines", on_delete=models.CASCADE)
    account = models.CharField(max_length=32, choices=Account.choices)
    debit = MoneyField()
    credit = MoneyField()

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.get_account_display()} D{self.debit}/C{self.credit}"


class AffiliatePayable(TimeStampedModel):
    """What we owe an affiliate for one farmed-out trip (APC-61). Record only — no money
    moves from here, and paying it in LimoAnywhere stays manual.

    One per confirmed farmed-out assignment, created when the order enters review
    (`payables.ensure_payables`). `expected_amount` tracks the trip review until the
    payable is approved, then stays as it was. Approving accrues the *invoice* amount;
    marking it paid clears that accrual (see `payables`).
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved"
        PAID = "paid", "Paid"

    class Method(models.TextChoices):
        CHECK = "check", "Check"
        ACH = "ach", "ACH"
        ZELLE = "zelle", "Zelle"
        OTHER = "other", "Other"

    STAGE_LABELS = {
        "invoice_missing": "Invoice missing",
        "awaiting_approval": "Awaiting approval",
        "approved": "Approved",
        "paid": "Paid",
    }

    assignment = models.OneToOneField(
        "dispatch.Assignment", related_name="payable", on_delete=models.PROTECT
    )
    expected_amount = MoneyField()
    invoice_number = models.CharField(max_length=64, blank=True)
    invoice_file = models.FileField(upload_to="affiliate-invoices/", blank=True)
    # Null = no invoice yet; a $0 invoice is still an invoice.
    invoice_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    invoice_received_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    # Required when approving with a variance: says why we're paying something other than
    # what we expected.
    approval_note = models.CharField(max_length=255, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="+",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    paid_method = models.CharField(max_length=20, choices=Method.choices, blank=True)
    paid_reference = models.CharField(max_length=120, blank=True)

    class Meta(TimeStampedModel.Meta):
        indexes = [models.Index(fields=["status"])]

    def __str__(self) -> str:
        return f"Payable · {self.assignment}"

    @property
    def variance(self) -> Decimal | None:
        """Invoice − expected; None until an invoice amount is on file."""
        if self.invoice_amount is None:
            return None
        return Decimal(self.invoice_amount) - Decimal(self.expected_amount)

    @property
    def stage(self) -> str:
        if self.status == self.Status.PAID:
            return "paid"
        if self.status == self.Status.APPROVED:
            return "approved"
        return "awaiting_approval" if self.invoice_amount is not None else "invoice_missing"

    @property
    def stage_label(self) -> str:
        return self.STAGE_LABELS[self.stage]
