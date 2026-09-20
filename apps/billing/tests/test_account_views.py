"""Setting up a billing account and its groups from the customer profile.

The endpoints are a thin skin over `apps/billing/services.py` — every rule that matters
(one account per contact, never no group, one default) is tested there. What is tested
here is the skin: that a service refusal reaches the office as a sentence it can act on
rather than a 500, that money-shaped changes are gated, and that the modals obey the
repo's UI rules.

They answer JSON rather than redirecting because the modal is Alpine: a redirect would
throw away what was typed on every validation error, and the rest of the portal
(`contact_update`, the dispatch endpoints, `reservation_save`) already posts this way.
"""

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.billing import services
from apps.billing.models import BillingAccount, Terms
from apps.contacts.factories import ContactFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def biller(client):
    """An agent who may touch money — everything here is a billing change."""
    user = UserFactory(can_manage_payments=True)
    client.force_login(user)
    return user


def _account(contact=None, **over):
    fields = {
        "name": "Ridgeline Partners",
        "terms": Terms.NET_30,
        "group_name": "Accounts Payable",
        "invoice_email": "ap@ridgeline.example",
    }
    return services.create_account(contact or ContactFactory(), **{**fields, **over})


def _create(client, contact, **over):
    data = {
        "name": "Ridgeline Partners",
        "terms": Terms.NET_30,
        "group_name": "Accounts Payable",
        "invoice_email": "ap@ridgeline.example",
        **over,
    }
    return client.post(reverse("billing_account_create", args=[contact.pk]), data)


# --- creating an account -------------------------------------------------------------


def test_creating_an_account_makes_it_and_its_first_group(client, biller):
    contact = ContactFactory()
    resp = _create(client, contact)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    account = BillingAccount.objects.get(contact=contact)
    assert account.name == "Ridgeline Partners"
    assert account.terms == Terms.NET_30
    group = account.groups.get()
    assert (group.name, group.invoice_email, group.is_default) == (
        "Accounts Payable",
        "ap@ridgeline.example",
        True,
    )


def test_the_po_flag_carries_through(client, biller):
    contact = ContactFactory()
    _create(client, contact, po_required="on")
    assert BillingAccount.objects.get(contact=contact).groups.get().po_required is True


def test_an_account_needs_a_name(client, biller):
    contact = ContactFactory()
    resp = _create(client, contact, name="   ")
    assert resp.status_code == 400
    assert "name" in resp.json()["errors"]
    assert not BillingAccount.objects.exists()


def test_the_first_group_needs_a_name(client, biller):
    """The account must not be created without something to invoice against."""
    contact = ContactFactory()
    resp = _create(client, contact, group_name="")
    assert resp.status_code == 400
    assert "group_name" in resp.json()["errors"]
    assert not BillingAccount.objects.exists()


def test_an_invoice_address_must_be_an_email(client, biller):
    resp = _create(client, ContactFactory(), invoice_email="not-an-address")
    assert resp.status_code == 400
    assert "invoice_email" in resp.json()["errors"]


def test_the_invoice_address_is_optional(client, biller):
    """It can be filled in later; refusing to create the account over it helps nobody."""
    contact = ContactFactory()
    assert _create(client, contact, invoice_email="").status_code == 200
    assert BillingAccount.objects.get(contact=contact).groups.get().invoice_email == ""


def test_a_second_account_is_refused_in_words_not_a_500(client, biller):
    contact = ContactFactory()
    _account(contact=contact)
    resp = _create(client, contact, name="Second")
    assert resp.status_code == 400
    assert "already has a billing account" in resp.json()["error"]
    assert BillingAccount.objects.filter(contact=contact).count() == 1


# --- editing the account -------------------------------------------------------------


def test_the_account_name_and_terms_can_be_changed(client, biller):
    account = _account()
    resp = client.post(
        reverse("billing_account_update", args=[account.pk]),
        {"name": "Ridgeline Partners LLC", "terms": Terms.NET_45},
    )
    assert resp.status_code == 200
    account.refresh_from_db()
    assert (account.name, account.terms) == ("Ridgeline Partners LLC", Terms.NET_45)


def test_the_account_cannot_be_renamed_to_nothing(client, biller):
    account = _account()
    resp = client.post(
        reverse("billing_account_update", args=[account.pk]), {"name": " ", "terms": Terms.NET_30}
    )
    assert resp.status_code == 400
    account.refresh_from_db()
    assert account.name == "Ridgeline Partners"


# --- adding a group ------------------------------------------------------------------


def test_a_group_can_be_added(client, biller):
    account = _account()
    resp = client.post(
        reverse("billing_group_add", args=[account.pk]),
        {"name": "Marketing — Events", "invoice_email": "events@ridgeline.example", "terms": ""},
    )
    assert resp.status_code == 200
    group = account.groups.get(name="Marketing — Events")
    assert group.is_default is False
    assert group.terms == ""  # inherits the account's


def test_a_group_can_be_given_its_own_terms(client, biller):
    account = _account()
    client.post(
        reverse("billing_group_add", args=[account.pk]),
        {"name": "Marketing", "terms": Terms.NET_15},
    )
    assert account.groups.get(name="Marketing").effective_terms == Terms.NET_15


def test_a_duplicate_group_name_is_refused_in_words(client, biller):
    account = _account()
    resp = client.post(
        reverse("billing_group_add", args=[account.pk]), {"name": "Accounts Payable"}
    )
    assert resp.status_code == 400
    assert "already has a group" in resp.json()["error"]
    assert account.groups.count() == 1


def test_a_group_needs_a_name(client, biller):
    account = _account()
    resp = client.post(reverse("billing_group_add", args=[account.pk]), {"name": "  "})
    assert resp.status_code == 400
    assert "name" in resp.json()["errors"]


# --- editing a group -----------------------------------------------------------------


def test_a_group_can_be_edited(client, biller):
    account = _account()
    group = account.groups.get()
    resp = client.post(
        reverse("billing_group_update", args=[group.pk]),
        {
            "name": "AP — Northeast",
            "invoice_email": "ne-ap@ridgeline.example",
            "terms": Terms.NET_15,
            "po_required": "on",
        },
    )
    assert resp.status_code == 200
    group.refresh_from_db()
    assert group.name == "AP — Northeast"
    assert group.invoice_email == "ne-ap@ridgeline.example"
    assert group.terms == Terms.NET_15
    assert group.po_required is True


def test_editing_a_group_keeps_it_the_default(client, biller):
    """Renaming the default must not quietly move it."""
    account = _account()
    group = account.groups.get()
    client.post(reverse("billing_group_update", args=[group.pk]), {"name": "AP"})
    group.refresh_from_db()
    assert group.is_default is True


def test_a_group_cannot_be_renamed_onto_another(client, biller):
    account = _account()
    other = services.add_group(account, name="Marketing")
    resp = client.post(
        reverse("billing_group_update", args=[other.pk]), {"name": "Accounts Payable"}
    )
    assert resp.status_code == 400
    other.refresh_from_db()
    assert other.name == "Marketing"


def test_a_group_can_keep_its_own_name(client, biller):
    """The duplicate check must not trip over the row being edited."""
    account = _account()
    group = account.groups.get()
    resp = client.post(
        reverse("billing_group_update", args=[group.pk]),
        {"name": "Accounts Payable", "invoice_email": "new-ap@ridgeline.example"},
    )
    assert resp.status_code == 200
    group.refresh_from_db()
    assert group.invoice_email == "new-ap@ridgeline.example"


# --- the default ---------------------------------------------------------------------


def test_a_group_can_be_made_the_default(client, biller):
    account = _account()
    second = services.add_group(account, name="Marketing")
    resp = client.post(reverse("billing_group_default", args=[second.pk]))
    assert resp.status_code == 200
    assert list(account.groups.filter(is_default=True)) == [second]


# --- removing a group ----------------------------------------------------------------


def test_a_group_can_be_removed(client, biller):
    account = _account()
    second = services.add_group(account, name="Marketing")
    resp = client.post(reverse("billing_group_delete", args=[second.pk]))
    assert resp.status_code == 200
    assert [g.name for g in account.groups.all()] == ["Accounts Payable"]


def test_the_last_group_cannot_be_removed(client, biller):
    account = _account()
    resp = client.post(reverse("billing_group_delete", args=[account.groups.get().pk]))
    assert resp.status_code == 400
    assert "last group" in resp.json()["error"]
    assert account.groups.count() == 1


def test_the_default_cannot_be_removed_while_it_is_the_default(client, biller):
    account = _account()
    services.add_group(account, name="Marketing")
    resp = client.post(
        reverse("billing_group_delete", args=[account.groups.get(is_default=True).pk])
    )
    assert resp.status_code == 400
    assert "default" in resp.json()["error"]
    assert account.groups.count() == 2


# --- who may do any of it ------------------------------------------------------------


def _all_endpoints(account) -> list:
    group = account.groups.get()
    return [
        (reverse("billing_account_create", args=[ContactFactory().pk]), {"name": "X"}),
        (reverse("billing_account_update", args=[account.pk]), {"name": "X"}),
        (reverse("billing_group_add", args=[account.pk]), {"name": "X"}),
        (reverse("billing_group_update", args=[group.pk]), {"name": "X"}),
        (reverse("billing_group_default", args=[group.pk]), {}),
        (reverse("billing_group_delete", args=[group.pk]), {}),
    ]


def test_an_agent_without_payments_access_cannot_change_billing(client):
    """Terms and who gets invoiced are money decisions."""
    account = _account()
    client.force_login(UserFactory(role=User.Role.AGENT, can_manage_payments=False))
    for url, data in _all_endpoints(account):
        assert client.post(url, data).status_code == 403, url


def test_an_owner_admin_may(client):
    account = _account()
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    resp = client.post(
        reverse("billing_account_update", args=[account.pk]),
        {"name": "Renamed", "terms": Terms.NET_30},
    )
    assert resp.status_code == 200


def test_signed_out_is_sent_to_the_login_page(client):
    account = _account()
    for url, data in _all_endpoints(account):
        resp = client.post(url, data)
        assert resp.status_code == 302 and "login" in resp["Location"], url


def test_none_of_them_answer_a_get(client, biller):
    """A billing change must never be something a link can do."""
    account = _account()
    for url, _ in _all_endpoints(account):
        assert client.get(url).status_code == 405, url


def test_reading_the_profile_still_needs_no_payments_access(client):
    """Seeing who a customer is stays open to every agent; changing their terms does not."""
    account = _account()
    client.force_login(UserFactory(can_manage_payments=False))
    body = client.get(reverse("contact_detail", args=[account.contact.pk])).content.decode()
    assert "Ridgeline Partners" in body


# --- the card shows the right controls ------------------------------------------------


def _profile(client, contact) -> str:
    return client.get(reverse("contact_detail", args=[contact.pk])).content.decode()


def test_the_only_group_offers_no_way_to_remove_it(client, biller):
    """The service refuses it, and a control that can only fail is worse than none."""
    account = _account()
    assert "Remove group" not in _profile(client, account.contact)


def test_a_second_group_can_be_removed_from_the_card(client, biller):
    account = _account()
    services.add_group(account, name="Marketing")
    assert "Remove group" in _profile(client, account.contact)


def test_an_agent_without_payments_access_is_shown_no_billing_controls(client):
    account = _account()
    services.add_group(account, name="Marketing")
    client.force_login(UserFactory(can_manage_payments=False))
    body = _profile(client, account.contact)
    for control in ("Add account group", "Remove group", "Make default"):
        assert control not in body, control


def test_the_group_terms_picker_names_what_blank_means(client, biller):
    """ "Optional" is not the same as "None" — leaving it blank inherits the account's, and
    the empty option has to say which terms those are."""
    account = _account(terms=Terms.NET_45)
    assert "Same as the account — Net 45" in _profile(client, account.contact)


def test_the_modals_are_on_the_page(client, biller):
    body = _profile(client, _account().contact)
    assert "New billing account" in body  # the edit form re-titles itself in Alpine
    assert "Add account group" in body


def test_the_alpine_attributes_survive_being_rendered(client, biller):
    """A stray `"` inside a double-quoted x-data closes the attribute and silently kills
    the whole component in the browser — no error, nothing works. It has bitten this repo
    before (see CLAUDE.local.md), and no Django test notices, so check the rendered text.
    """
    import re

    account = _account()
    services.add_group(account, name='Marketing "Events"')  # a name that must be escaped
    body = _profile(client, account.contact)

    x_data = re.search(r'x-data="(billingCard\(.*?\))"', body, re.S)
    assert x_data, "the billing card lost its x-data"
    assert '"' not in x_data.group(1)
    for key in ("initialTab:", "accountTerms:", "create:", "update:", "addGroup:"):
        assert key in x_data.group(1), key

    # and the row handlers, which carry a customer-supplied name through json_string
    handler = re.search(r'@click="(openGroup\(\{ id: \d+.*?\})\)"', body, re.S)
    assert handler and '"' not in handler.group(1)
    assert "&quot;Marketing \\&quot;Events\\&quot;&quot;" in body


# --- the house rules ------------------------------------------------------------------

MODALS = ("_account_modal.html", "_group_modal.html")


def _markup(name: str) -> str:
    """A modal's markup with its `{% comment %}` blocks stripped.

    The comments explain these very rules, so a naive substring search finds the word in
    the explanation and passes — or fails — for the wrong reason.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    source = (root / "templates" / "billing" / name).read_text()
    return re.sub(r"{% comment %}.*?{% endcomment %}", "", source, flags=re.S)


def test_the_modals_use_no_native_select_and_no_browser_confirm():
    for name in MODALS:
        source = _markup(name)
        assert "window.confirm" not in source, name
        assert "<dialog" not in source, name
        # a bare <select> is the rule; the shared component renders `select ... data-tom`
        assert "<select" not in source, name


def test_the_modals_carry_no_transition():
    """A transition left an invisible backdrop eating clicks — see _reservation_editor.html."""
    for name in MODALS:
        assert "x-transition" not in _markup(name), name


def test_each_modal_shows_and_hides_as_one():
    """One x-show, on the outer container. Alpine defers a parent's hide to a child's, and
    a second x-show inside left the overlay up with nothing visible in it."""
    for name in MODALS:
        assert _markup(name).count('x-show="modal ===') == 1, name
