from django.db import models


class Channel(models.TextChoices):
    """Lead source channel — shared by Contact and Lead."""

    WEBSITE = "website", "Website"
    WEDDING_PRO = "wedding_pro", "Wedding Pro"
    PHONE = "phone", "Phone"
    API = "api", "API"


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
