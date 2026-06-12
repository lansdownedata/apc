from django.contrib import admin

from .models import Charge, PaymentPlan


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
