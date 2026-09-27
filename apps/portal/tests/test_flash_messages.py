"""Flash messages surface on the page they were meant for, never on a later one.

Django only drains a message when a template iterates `messages`. Pages that don't render
them inline leave the message queued in the session, and it then pops up on whatever
page next does — e.g. "Edit insurance policy saved." appearing on an unrelated lead.
"""

import pytest
from django.urls import reverse
from django.utils.html import escape

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.accounts.tests.test_views import _accept_url, _make_pending
from apps.leads.factories import LeadFactory
from apps.vendors.factories import VendorInsuranceFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def agent(django_user_model):
    return django_user_model.objects.create_superuser(
        username="agent", email="agent@example.com", password="pw"
    )


def _edit_policy(client, policy):
    return client.post(
        reverse("insurance_edit", args=[policy.pk]),
        {
            "insurer": policy.insurer,
            "policy_number": policy.policy_number,
            "coverage_amount": policy.coverage_amount,
            "effective_date": policy.effective_date.isoformat(),
            "expiry_date": policy.expiry_date.isoformat(),
        },
        follow=True,
    )


def test_message_shows_on_the_page_it_redirects_to(client, agent):
    client.force_login(agent)
    resp = _edit_policy(client, VendorInsuranceFactory())
    assert resp.redirect_chain, "the edit should have saved and redirected"
    assert "Edit insurance policy saved." in resp.content.decode()


def test_message_does_not_leak_onto_a_later_page(client, agent):
    client.force_login(agent)
    _edit_policy(client, VendorInsuranceFactory())
    resp = client.get(reverse("lead_detail", args=[LeadFactory().pk]))
    assert "Edit insurance policy saved." not in resp.content.decode()


def test_a_page_with_inline_messages_does_not_also_toast_them(client, agent):
    """The lead page renders its messages as banners; the shell must not repeat them."""
    client.force_login(agent)
    lead = LeadFactory()  # a NEW lead has nothing to reissue, so this flashes an error
    resp = client.post(reverse("lead_reissue_quote", args=[lead.pk]), follow=True)
    texts = [str(m) for m in resp.context["messages"]]
    assert len(texts) == 1
    assert resp.content.decode().count(escape(texts[0])) == 1


def test_accept_invite_message_shows_on_the_sign_in_page(client):
    user = _make_pending(UserFactory(role=User.Role.OWNER_ADMIN))
    resp = client.post(
        _accept_url(user),
        {"new_password1": "sW9!kdo2Lm", "new_password2": "sW9!kdo2Lm"},
        follow=True,
    )
    assert "Password set" in resp.content.decode()
