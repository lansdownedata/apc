from django import forms
from django.contrib import admin

from .models import AffiliatePayable, Charge, JournalEntry, JournalLine, PaymentPlan


class ChargeInline(admin.TabularInline):
    model = Charge
    extra = 0
    readonly_fields = ("created_at",)


@admin.register(PaymentPlan)
class PaymentPlanAdmin(admin.ModelAdmin):
    list_display = (
        "lead",
        "deposit_pct",
        "quote_total",
        "deposit_status",
        "balance_status",
        "card_last4",
    )
    list_filter = ("deposit_status", "balance_status", "processor")
    search_fields = ("lead__contact__name", "stripe_customer_id")
    inlines = [ChargeInline]


@admin.register(Charge)
class ChargeAdmin(admin.ModelAdmin):
    list_display = ("plan", "kind", "amount", "status", "attempt_no", "attempted_at")
    list_filter = ("kind", "status")
    search_fields = ("idempotency_key", "stripe_payment_intent_id")
    raw_id_fields = ("plan", "reservation")


class JournalLineInline(admin.TabularInline):
    model = JournalLine
    extra = 0
    readonly_fields = ("account", "debit", "credit", "created_at")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = ("kind", "lead", "reservation", "source", "memo", "posted_at")
    list_filter = ("kind", "source")
    search_fields = ("idempotency_key", "lead__contact__name", "memo")
    inlines = [JournalLineInline]
    readonly_fields = (
        "kind",
        "lead",
        "reservation",
        "source",
        "memo",
        "charge",
        "stripe_ref",
        "created_by",
        "idempotency_key",
        "posted_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


APPROVED_LOCKED = (
    "Approved payables can't change amount here; corrections come with Trip Processing."
)


class AffiliatePayableAdminForm(forms.ModelForm):
    """Only a draft's invoice can be corrected here. Once approved, the ledger has accrued
    (and maybe paid) that amount and QuickBooks holds it as a bill; an edit here would drift
    both away from the portal (Moe's default, APC-74 review)."""

    class Meta:
        model = AffiliatePayable
        fields = (
            "invoice_number",
            "invoice_file",
            "invoice_amount",
            "invoice_received_at",
            "approval_note",
        )

    def clean(self):
        data = super().clean()
        billed = self.instance.status in (
            AffiliatePayable.Status.APPROVED,
            AffiliatePayable.Status.PAID,
        )
        if billed:
            for field in self.changed_data:
                self.add_error(field, APPROVED_LOCKED)
        return data


@admin.register(AffiliatePayable)
class AffiliatePayableAdmin(admin.ModelAdmin):
    """Read-mostly: approving and paying post ledger entries, so they happen through the
    trip review (`payables`), never by editing a status here."""

    form = AffiliatePayableAdminForm
    list_display = ("assignment", "status", "expected_amount", "invoice_amount", "paid_at")
    list_filter = ("status", "paid_method")
    search_fields = ("invoice_number", "assignment__vendor__name")
    list_select_related = ("assignment__vendor", "assignment__reservation")
    readonly_fields = (
        "assignment",
        "status",
        "expected_amount",
        "approved_by",
        "approved_at",
        "paid_at",
        "paid_method",
        "paid_reference",
        # APC-74: the Bill and BillPayment links are the sync's to write, never a hand edit.
        "qbo_id",
        "qbo_sync_token",
        "qbo_realm_id",
        "qbo_sync_state",
        "qbo_sync_error",
        "qbo_synced_at",
        "qbo_attachment_id",
        "qbo_attachment_sync_state",
        "qbo_attachment_sync_error",
        "qbo_payment_id",
        "qbo_payment_sync_token",
        "qbo_payment_realm_id",
        "qbo_payment_sync_state",
        "qbo_payment_sync_error",
        "qbo_payment_synced_at",
    )
