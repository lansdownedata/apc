"""Billing accounts — a customer on terms, invoiced, instead of paying by card.

The shape mirrors QuickBooks Online, because that is where these records have to land:
a `BillingAccount` is the **Customer**, and each `AccountGroup` under it is a
**sub-customer** — the department an invoice is actually made out to ("Accounts Payable",
"Marketing — Events"). Invoices bill on the group, never rolled up to the parent.

Nothing here talks to QuickBooks. The `qbo_*` fields exist from the start so the sync in
Phase 2 is a service and a cron job rather than a migration on live billing data.
"""

from django.conf import settings
from django.db import models

from apps.core.fields import MoneyField
from apps.core.models import TimeStampedModel


class Terms(models.TextChoices):
    """Payment terms, as a plain string.

    Deliberately not a foreign key to a terms table: Phase 2 replaces this picker with the
    client's own terms list out of QuickBooks, matched by name. Keeping the stored value a
    string means that swap is a form change, not a data migration.
    """

    DUE_ON_RECEIPT = "due_on_receipt", "Due on receipt"
    NET_15 = "net_15", "Net 15"
    NET_30 = "net_30", "Net 30"
    NET_45 = "net_45", "Net 45"


class SyncState(models.TextChoices):
    NOT_SYNCED = "not_synced", "Not synced"
    SYNCED = "synced", "Synced"
    ERROR = "error", "Not synced"


class QboSyncedModel(models.Model):
    """What every record that has a counterpart in QuickBooks carries.

    Two users today (account, group) and a third in Phase 4 (invoice). The error is kept
    as a sentence a dispatcher can act on — "QuickBooks already has a customer with this
    name" — not the API body, because it is rendered straight onto the profile.
    """

    # The QuickBooks record's Id (a Customer for accounts and groups; APC-74 reuses the mixin
    # for vendors and bills, hence the neutral name) and its SyncToken, which every update
    # must send. Both blank until the first successful push (APC-42).
    qbo_id = models.CharField(max_length=64, blank=True)
    qbo_sync_token = models.CharField(max_length=32, blank=True)
    # Which QuickBooks company `qbo_id` belongs to: connect a different company (sandbox, then
    # the client's live one) and the record is pushed to it as new.
    qbo_realm_id = models.CharField(max_length=32, blank=True)
    qbo_sync_state = models.CharField(
        max_length=20, choices=SyncState.choices, default=SyncState.NOT_SYNCED
    )
    qbo_sync_error = models.CharField(max_length=255, blank=True)
    qbo_synced_at = models.DateTimeField(null=True, blank=True)
    # Which create this is, for Intuit's requestid: bumped ONLY when QuickBooks itself refuses
    # a create. A refusal made here (a missing term or account choice) sends nothing, so it
    # must not change the id — a lost-response create retried later has to replay, not
    # duplicate (APC-74 review).
    qbo_create_attempt = models.PositiveSmallIntegerField(default=0, editable=False)

    class Meta:
        abstract = True


class BillingAccount(QboSyncedModel, TimeStampedModel):
    """One customer's terms account. Becomes the Customer in QuickBooks."""

    # A FK, not a OneToOne, and the "one per contact" rule is a constraint in Meta rather
    # than `unique=True` on the field. Two reasons, both about the day the client wants
    # several accounts per customer (2026-09-19 he chose one "to make it easier"):
    # dropping a named constraint is a migration and nothing else, and every call site
    # already reads `contact.billing_accounts` — a manager — so none of them change.
    # `unique=True` here would also raise Django's W342, telling us to use a OneToOne,
    # which is the shape that WOULD make lifting it a rewrite.
    contact = models.ForeignKey(
        "contacts.Contact",
        on_delete=models.PROTECT,  # billing history must not vanish with a contact
        related_name="billing_accounts",
    )
    name = models.CharField(max_length=200)  # the Customer DisplayName in QBO
    terms = models.CharField(max_length=20, choices=Terms.choices, default=Terms.NET_30)

    class Meta(TimeStampedModel.Meta):
        constraints = [
            models.UniqueConstraint(fields=["contact"], name="uniq_billing_account_per_contact"),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def default_group(self):
        """The group an order bills to unless it says otherwise.

        Reads through `groups.all()` rather than `.filter()` so a caller that prefetched
        pays no extra query — the same reason `dispatch.selectors.confirmed_assignment`
        does it that way.
        """
        for group in self.groups.all():
            if group.is_default:
                return group
        return None


class AccountGroup(QboSyncedModel, TimeStampedModel):
    """Who an invoice is made out to. A sub-customer in QuickBooks."""

    account = models.ForeignKey(BillingAccount, on_delete=models.CASCADE, related_name="groups")
    name = models.CharField(max_length=200)
    invoice_email = models.EmailField(blank=True)
    # Blank means "whatever the account says" — see `effective_terms`.
    terms = models.CharField(max_length=20, choices=Terms.choices, blank=True)
    po_required = models.BooleanField(default=False)
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_default", "name"]
        constraints = [
            # Two groups with one name would collide in QuickBooks anyway.
            models.UniqueConstraint(fields=["account", "name"], name="uniq_group_name_per_account"),
        ]
        # NOTE: "exactly one default per account" is NOT a constraint here. It needs a
        # conditional unique index, and MySQL — the local and test database — has no
        # partial indexes, so it would exist on prod Postgres and silently not exist
        # where the tests run. `services.set_default_group` enforces it under
        # select_for_update(), the same way dispatch's one-active-assignment rule does.

    def __str__(self) -> str:
        return f"{self.account.name} — {self.name}"

    @property
    def effective_terms(self) -> str:
        return self.terms or self.account.terms


class Invoice(QboSyncedModel, TimeStampedModel):
    """A bill made out to one group, covering a set of trips (APC-106). Becomes the
    Invoice in QuickBooks. Never edited once made: a wrong one is voided and replaced."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        VOID = "void", "Void"

    group = models.ForeignKey(AccountGroup, on_delete=models.PROTECT, related_name="invoices")
    # QuickBooks' DocNumber. Blank until the push assigns it (APC-107).
    number = models.CharField(max_length=21, blank=True)
    invoice_date = models.DateField()
    due_date = models.DateField()
    po_number = models.CharField(max_length=64, blank=True)
    total = MoneyField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    memo = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    void_reason = models.CharField(max_length=255, blank=True)
    # The invoice issued in this one's place after a void.
    replaced_by = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # Lease taken by the QuickBooks push so two workers never push one invoice (APC-107).
    qbo_push_started_at = models.DateTimeField(null=True, blank=True)
    # Set when a portal void must also void the QuickBooks invoice; cleared never, the sync
    # state says whether it landed.
    qbo_void_requested_at = models.DateTimeField(null=True, blank=True)
    # An open invoice's date, PO and memo can be corrected (APC-109); the last edit is stamped.
    edited_at = models.DateTimeField(null=True, blank=True)
    edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    edit_note = models.CharField(max_length=255, blank=True)
    # Set when an edit of an invoice already in QuickBooks must be sent there too.
    qbo_edit_requested_at = models.DateTimeField(null=True, blank=True)
    # The date QuickBooks holds the invoice under, from its last answer: what its closed
    # period is judged by until an edit lands there (APC-109).
    qbo_txn_date = models.DateField(null=True, blank=True)

    class Meta(TimeStampedModel.Meta):
        ordering = ["-invoice_date", "-pk"]

    def __str__(self) -> str:
        return self.label

    @property
    def label(self) -> str:
        return f"Invoice {self.number}" if self.number else f"Invoice (draft #{self.pk})"


class InvoiceLine(TimeStampedModel):
    """One trip on an invoice. The OneToOne is the rule that a trip is billed once."""

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    reservation = models.OneToOneField(
        "reservations.Reservation", on_delete=models.PROTECT, related_name="invoice_line"
    )
    description = models.TextField()
    amount = MoneyField()

    class Meta(TimeStampedModel.Meta):
        ordering = ["pk"]


class CustomerLink(QboSyncedModel, TimeStampedModel):
    """A card-paying contact's QuickBooks Customer (APC-108). A contact who owns a
    BillingAccount already in QuickBooks reuses that Customer and never gets a link."""

    contact = models.OneToOneField(
        "contacts.Contact", on_delete=models.CASCADE, related_name="qbo_customer"
    )
    display_name = models.CharField(max_length=100)
    # Lease taken by the QuickBooks customer push (APC-108).
    qbo_push_started_at = models.DateTimeField(null=True, blank=True)

    class Meta(TimeStampedModel.Meta):
        pass

    def __str__(self) -> str:
        return self.display_name


class QboTerm(TimeStampedModel):
    """A payment term as it exists in the connected QuickBooks company (APC-42) — a cache,
    refreshed by the `retry-qbo-sync` cron and the Settings action, never fetched per page.

    Our own `Terms` are matched to these by name when a customer is pushed; a term QuickBooks
    doesn't have is left off the customer and listed on Settings, never guessed.
    """

    qbo_id = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=100)
    active = models.BooleanField(default=True)

    class Meta(TimeStampedModel.Meta):
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class QboAccount(TimeStampedModel):
    """An account from the connected company's chart of accounts (APC-74) — a cache, like
    `QboTerm`, refreshed by the hourly sync and the Settings action.

    Only the two kinds the portal can use are kept: expense accounts (where an affiliate bill
    posts) and bank accounts (where a bill payment comes from). Keyed by company as well as
    id, because QuickBooks numbers each company's accounts from 1.
    """

    EXPENSE_TYPES = ("Expense", "Cost of Goods Sold", "Other Expense")
    PAYMENT_TYPES = ("Bank",)
    # Undeposited Funds is an "Other Current Asset".
    DEPOSIT_TYPES = ("Bank", "Other Current Asset")

    realm_id = models.CharField(max_length=32)
    qbo_id = models.CharField(max_length=32)
    name = models.CharField(max_length=255)
    account_type = models.CharField(max_length=40)
    active = models.BooleanField(default=True)

    class Meta(TimeStampedModel.Meta):
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["realm_id", "qbo_id"], name="uniq_qbo_account"),
        ]

    def __str__(self) -> str:
        return self.name


class QboItem(TimeStampedModel):
    """A product/service item from the connected company (APC-107) — a cache, like
    `QboAccount`, refreshed by the hourly sync and the Settings action.

    Only the kinds an invoice line can use are kept (`ITEM_TYPES`). Keyed by company as well
    as id, because QuickBooks numbers each company's items from 1.
    """

    ITEM_TYPES = ("Service", "NonInventory")

    realm_id = models.CharField(max_length=32)
    qbo_id = models.CharField(max_length=32)
    name = models.CharField(max_length=255)
    item_type = models.CharField(max_length=40)
    active = models.BooleanField(default=True)

    class Meta(TimeStampedModel.Meta):
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["realm_id", "qbo_id"], name="uniq_qbo_item"),
        ]

    def __str__(self) -> str:
        return self.name


class QboAccountMapping(models.Model):
    """Which QuickBooks accounts affiliate bills post to and pay from (APC-74). A singleton,
    chosen on Settings → QuickBooks. Until a choice is made the matching push refuses with a
    sentence saying so; it never guesses an account."""

    expense_account = models.ForeignKey(
        QboAccount, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    payment_account = models.ForeignKey(
        QboAccount, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # Payables approved on or after this date (trip-local) go to QuickBooks; earlier ones
    # never do, so the first sync doesn't back-date every past affiliate bill into the books.
    # Blank = no bills or payments go at all.
    bills_from = models.DateField("push affiliate bills from", null=True, blank=True)
    # The item every invoice line posts under, and the date (trip-local) from which customer
    # invoices go to QuickBooks; earlier ones never do. Blank = no customer documents go at all.
    trip_item = models.ForeignKey(
        QboItem, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    customer_docs_from = models.DateField("push customer documents from", null=True, blank=True)
    # Where card charges and refunds deposit (APC-108).
    sales_deposit_account = models.ForeignKey(
        QboAccount, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # Invoices dated on or before this are in a period the accountant closed in QuickBooks, so
    # the portal refuses to edit them (APC-109). Blank = no period is closed.
    book_close_date = models.DateField("QuickBooks books closed through", null=True, blank=True)

    class Meta:
        verbose_name = "QuickBooks account mapping"

    def __str__(self) -> str:
        return "QuickBooks account mapping"

    @classmethod
    def load(cls) -> "QboAccountMapping":
        return cls.objects.get_or_create(pk=1)[0]
