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


@admin.register(AffiliatePayable)
class AffiliatePayableAdmin(admin.ModelAdmin):
    """Read-mostly: approving and paying post ledger entries, so they happen through the
    trip review (`payables`), never by editing a status here."""

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
    )
