"""APC-66: the staff side — Invite / Resend / Revoke on the contact profile."""

from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.core import outbound
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead, LeadContact
from apps.planners import services
from apps.planners.models import PlannerAccount

pytestmark = pytest.mark.django_db

SEND = "apps.planners.services.send_html_email"


@pytest.fixture
def staff(client):
    client.force_login(UserFactory())
    return client


@pytest.fixture
def contact():
    return ContactFactory(name="Dana Planner", email="dana@example.com")


def _profile(client, contact) -> str:
    response = client.get(reverse("contact_detail", args=[contact.pk]))
    assert response.status_code == 200
    return response.content.decode()


def test_the_profile_offers_an_invite_to_someone_without_access(staff, contact):
    html = _profile(staff, contact)
    assert "Planner access" in html
    assert "Not invited" in html
    assert reverse("planner_invite", args=[contact.pk]) in html


def test_without_an_email_the_invite_is_explained_not_offered(staff):
    contact = ContactFactory(email=None)
    html = _profile(staff, contact)
    assert "Add an email to invite" in html
    assert reverse("planner_invite", args=[contact.pk]) not in html


def test_invite_from_the_profile(staff, contact):
    with patch(SEND, return_value=True) as send:
        response = staff.post(reverse("planner_invite", args=[contact.pk]))
    assert response.status_code == 302
    assert response.url == reverse("contact_detail", args=[contact.pk])
    send.assert_called_once()
    account = PlannerAccount.objects.get(contact=contact)
    assert account.status == PlannerAccount.Status.INVITED
    html = _profile(staff, contact)
    assert "Invited" in html
    assert reverse("planner_resend", args=[contact.pk]) in html
    assert reverse("planner_revoke", args=[contact.pk]) in html


def test_invite_is_refused_while_outbound_is_off_and_the_state_still_shows(
    staff, contact, settings
):
    with patch(SEND, return_value=True):
        services.invite(contact, actor=UserFactory())
    other = ContactFactory(email="second@example.com")
    settings.OUTBOUND_ENABLED = False
    with patch(SEND) as send:
        response = staff.post(reverse("planner_invite", args=[other.pk]), follow=True)
    send.assert_not_called()
    assert not PlannerAccount.objects.filter(contact=other).exists()
    assert outbound.OFF_MESSAGE in response.content.decode()
    # the first contact's state renders regardless of the switch
    assert "Invited" in _profile(staff, contact)


def test_resend_and_revoke(staff, contact):
    with patch(SEND, return_value=True):
        services.invite(contact, actor=UserFactory())
        assert staff.post(reverse("planner_resend", args=[contact.pk])).status_code == 302
    staff.post(reverse("planner_revoke", args=[contact.pk]))
    account = PlannerAccount.objects.get(contact=contact)
    assert account.status == PlannerAccount.Status.REVOKED
    html = _profile(staff, contact)
    assert "Access revoked" in html
    assert reverse("planner_invite", args=[contact.pk]) in html  # re-invite


def test_revoke_confirms_through_the_app_modal(staff, contact):
    with patch(SEND, return_value=True):
        services.invite(contact, actor=UserFactory())
    html = _profile(staff, contact)
    assert "$store.modal.confirm" in html
    assert "Revoke access" in html


def test_registered_state_names_when_and_how_many_weddings(staff, contact):
    with patch(SEND, return_value=True):
        account, _ = services.invite(contact, actor=UserFactory())
    services.register(account, "a-long-pass-phrase-77")
    lead = LeadFactory(status=Lead.Status.BOOKED)
    LeadContact.objects.create(lead=lead, contact=contact, role=LeadContact.Role.LEAD_PLANNER)
    html = _profile(staff, contact)
    assert "Registered" in html
    assert "Sees 1 wedding" in html


def test_the_actions_are_staff_only_and_post_only(client, contact):
    url = reverse("planner_invite", args=[contact.pk])
    response = client.post(url)
    assert response.status_code == 302 and "/login/" in response.url
    client.force_login(UserFactory())
    assert client.get(url).status_code == 405
