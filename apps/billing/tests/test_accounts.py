"""A customer's billing account and the groups invoices are made out to.

An account is the customer in QuickBooks; each group under it is a sub-customer, and a
group is who an invoice is addressed to. The rules that matter are structural: a contact
has at most one account, and an account is never left with nothing to invoice against.

Both live in `services`, not in the database. "One default group per account" is the sort
of rule a conditional `UniqueConstraint` would express, and that is exactly the trap
`dispatch/services.py` documents at the top of the file — a `condition=` constraint exists
on prod Postgres and silently does not on the MySQL the tests run against.
"""

import pytest
from django.db import IntegrityError
from django.db.models import ProtectedError

from apps.billing import services
from apps.billing.factories import AccountGroupFactory, BillingAccountFactory
from apps.billing.models import AccountGroup, BillingAccount, SyncState, Terms
from apps.contacts.factories import ContactFactory

pytestmark = pytest.mark.django_db


def _account(**over):
    """An account through the service — the only door that makes a valid one."""
    fields = {
        "name": "Ridgeline Partners",
        "terms": Terms.NET_30,
        "group_name": "Accounts Payable",
        "invoice_email": "ap@ridgeline.example",
    }
    contact = over.pop("contact", None) or ContactFactory()
    return services.create_account(contact, **{**fields, **over})


# --- creating one --------------------------------------------------------------------


def test_creating_an_account_creates_its_first_group_as_the_default():
    """ "At least one group" is made impossible to violate rather than validated later."""
    account = _account()
    group = account.groups.get()
    assert group.name == "Accounts Payable"
    assert group.invoice_email == "ap@ridgeline.example"
    assert group.is_default is True


def test_the_account_and_its_group_arrive_together_or_not_at_all():
    """A blank group name must not leave an account behind with nothing to invoice."""
    contact = ContactFactory()
    with pytest.raises(services.BillingError):
        services.create_account(contact, name="Ridgeline", terms=Terms.NET_30, group_name="  ")
    assert not BillingAccount.objects.filter(contact=contact).exists()
    assert not AccountGroup.objects.exists()


def test_a_contact_gets_one_account_for_now():
    """Lifting this later must be dropping a constraint, not a remodel."""
    contact = ContactFactory()
    _account(contact=contact)
    with pytest.raises(services.BillingError, match="already has a billing account"):
        _account(contact=contact, name="Second")


def test_two_contacts_can_each_have_their_own():
    _account(contact=ContactFactory(), name="Ridgeline")
    _account(contact=ContactFactory(), name="Beltway")
    assert BillingAccount.objects.count() == 2


def test_an_account_needs_a_name():
    with pytest.raises(services.BillingError):
        _account(name="   ")


# --- groups --------------------------------------------------------------------------


def test_a_second_group_is_not_the_default():
    account = _account()
    group = services.add_group(account, name="Marketing — Events")
    assert group.is_default is False
    assert account.groups.filter(is_default=True).count() == 1


def test_two_groups_on_one_account_cannot_share_a_name():
    """They would collide in QuickBooks anyway."""
    account = _account()
    with pytest.raises(services.BillingError, match="already has a group"):
        services.add_group(account, name="Accounts Payable")


def test_the_same_group_name_on_different_accounts_is_fine():
    services.add_group(_account(contact=ContactFactory()), name="Ops")
    services.add_group(_account(contact=ContactFactory()), name="Ops")
    assert AccountGroup.objects.filter(name="Ops").count() == 2


def test_a_group_name_is_required():
    with pytest.raises(services.BillingError):
        services.add_group(_account(), name=" ")


# --- the default ---------------------------------------------------------------------


def test_setting_a_default_leaves_exactly_one():
    account = _account()
    second = services.add_group(account, name="Marketing")
    services.set_default_group(second)
    defaults = list(account.groups.filter(is_default=True))
    assert defaults == [second]


def test_a_group_can_only_be_made_default_on_its_own_account():
    other = services.add_group(_account(contact=ContactFactory()), name="Ops")
    account = _account(contact=ContactFactory())
    services.set_default_group(other)  # legitimate on its own account
    assert account.groups.get().is_default is True  # and leaves this one alone


# --- deleting ------------------------------------------------------------------------


def test_the_last_group_cannot_be_deleted():
    account = _account()
    with pytest.raises(services.BillingError, match="last group"):
        services.delete_group(account.groups.get())
    assert account.groups.count() == 1


def test_the_default_cannot_be_deleted_while_it_is_the_default():
    account = _account()
    services.add_group(account, name="Marketing")
    with pytest.raises(services.BillingError, match="default"):
        services.delete_group(account.groups.get(is_default=True))
    assert account.groups.count() == 2


def test_a_group_can_be_deleted_once_another_is_the_default():
    account = _account()
    second = services.add_group(account, name="Marketing")
    services.set_default_group(second)
    services.delete_group(account.groups.get(is_default=False))
    assert [g.name for g in account.groups.all()] == ["Marketing"]


# --- terms ---------------------------------------------------------------------------


def test_a_group_inherits_the_accounts_terms_when_it_sets_none():
    account = _account(terms=Terms.NET_30)
    assert account.groups.get().effective_terms == Terms.NET_30


def test_a_group_can_override_the_terms():
    account = _account(terms=Terms.NET_30)
    group = services.add_group(account, name="Marketing", terms=Terms.NET_15)
    assert group.effective_terms == Terms.NET_15


# --- QuickBooks fields exist but are untouched in Phase 1 ----------------------------


def test_nothing_is_synced_yet():
    account = _account()
    group = account.groups.get()
    for record in (account, group):
        assert record.qbo_sync_state == SyncState.NOT_SYNCED
        assert record.qbo_customer_id == ""
        assert record.qbo_synced_at is None
        assert record.qbo_sync_error == ""


# --- the contact ---------------------------------------------------------------------


def test_a_contact_with_billing_history_cannot_be_deleted():
    """Billing history must not vanish with a contact."""
    contact = ContactFactory()
    _account(contact=contact)
    with pytest.raises(ProtectedError):
        contact.delete()


def test_deleting_an_account_takes_its_groups():
    account = _account()
    services.add_group(account, name="Marketing")
    account.delete()
    assert not AccountGroup.objects.exists()


# --- the factories the later tickets build on ----------------------------------------


def test_the_factories_make_a_usable_account_and_group():
    account = BillingAccountFactory()
    group = AccountGroupFactory(account=account)
    assert account.contact is not None
    assert group.account == account


def test_the_database_still_refuses_a_duplicate_group_name():
    """The service checks it first; the constraint is the backstop."""
    account = _account()
    with pytest.raises(IntegrityError):
        AccountGroup.objects.create(account=account, name="Accounts Payable")
