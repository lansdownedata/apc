"""Billing accounts — a customer on terms, invoiced, instead of paying by card.

The shape mirrors QuickBooks Online, because that is where these records have to land:
a `BillingAccount` is the **Customer**, and each `AccountGroup` under it is a
**sub-customer** — the department an invoice is actually made out to ("Accounts Payable",
"Marketing — Events"). Invoices bill on the group, never rolled up to the parent.

Nothing here talks to QuickBooks. The `qbo_*` fields exist from the start so the sync in
Phase 2 is a service and a cron job rather than a migration on live billing data.
"""

from django.db import models

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

    qbo_customer_id = models.CharField(max_length=64, blank=True)
    qbo_sync_state = models.CharField(
        max_length=20, choices=SyncState.choices, default=SyncState.NOT_SYNCED
    )
    qbo_sync_error = models.CharField(max_length=255, blank=True)
    qbo_synced_at = models.DateTimeField(null=True, blank=True)

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
