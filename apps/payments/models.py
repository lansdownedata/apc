from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models

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

    def record_charge(self, *, kind: str, amount: Decimal) -> "Charge":
        """Create the next Charge attempt for this plan with a derived idempotency key."""
        attempt = self.charges.filter(kind=kind).count() + 1
        return self.charges.create(
            kind=kind,
            amount=amount,
            attempt_no=attempt,
            idempotency_key=f"plan{self.pk}-{kind}-{attempt}",
        )

    def __str__(self) -> str:
        return f"Payment plan · {self.lead.quote_no}"


class Charge(TimeStampedModel):
    """A single Stripe charge attempt (deposit or balance) — idempotent + auditable."""

    class Kind(models.TextChoices):
        DEPOSIT = "deposit", "Deposit"
        BALANCE = "balance", "Balance"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    plan = models.ForeignKey(PaymentPlan, related_name="charges", on_delete=models.CASCADE)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    amount = MoneyField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    stripe_payment_intent_id = models.CharField(max_length=64, blank=True)
    idempotency_key = models.CharField(max_length=120, unique=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    attempt_no = models.PositiveSmallIntegerField(default=1)
    attempted_at = models.DateTimeField(null=True, blank=True)

    @property
    def succeeded(self) -> bool:
        return self.status == self.Status.SUCCEEDED

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.amount} · {self.get_status_display()}"
