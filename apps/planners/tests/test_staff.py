"""APC-66: the staff side — Invite / Resend / Revoke on the contact profile."""

from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.audit.models import AuditEntry
from apps.contacts.factories import ContactFactory
from apps.core import outbound
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead, LeadContact
from apps.planners import services
from apps.planners.models import PlannerAccount

pytestmark = pytest.mark.django_db

SEND = "apps.notifications.email.deliver_html_email"


@pytest.fixture
def staff(client):
    client.force_login(UserFactory())
    return client


@pytest.fixture
def contact():
    return ContactFactory(name="Dana Planner", email="dana@example.com", phone="(571) 555-0142")


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
    assert "Add an email and a mobile number to invite them" in html
    assert reverse("planner_invite", args=[contact.pk]) not in html


def test_invite_from_the_profile(staff, contact):
    with patch(SEND, return_value="pm-1") as send:
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
    with patch(SEND, return_value="pm-1"):
        services.invite(contact, actor=UserFactory())
    other = ContactFactory(email="second@example.com", phone="(571) 555-0142")
    settings.OUTBOUND_ENABLED = False
    with patch(SEND) as send:
        response = staff.post(reverse("planner_invite", args=[other.pk]), follow=True)
    send.assert_not_called()
    assert not PlannerAccount.objects.filter(contact=other).exists()
    assert outbound.OFF_MESSAGE in response.content.decode()
    # the first contact's state renders regardless of the switch
    assert "Invited" in _profile(staff, contact)


def test_resend_and_revoke(staff, contact):
    with patch(SEND, return_value="pm-1"):
        services.invite(contact, actor=UserFactory())
        assert staff.post(reverse("planner_resend", args=[contact.pk])).status_code == 302
    staff.post(reverse("planner_revoke", args=[contact.pk]))
    account = PlannerAccount.objects.get(contact=contact)
    assert account.status == PlannerAccount.Status.REVOKED
    html = _profile(staff, contact)
    assert "Access revoked" in html
    assert reverse("planner_invite", args=[contact.pk]) in html  # re-invite


def test_revoke_confirms_through_the_app_modal(staff, contact):
    with patch(SEND, return_value="pm-1"):
        services.invite(contact, actor=UserFactory())
    html = _profile(staff, contact)
    assert "$store.modal.confirm" in html
    assert "Revoke access" in html


def test_registered_state_names_when_and_how_many_weddings(staff, contact):
    with patch(SEND, return_value="pm-1"):
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


def _registered(contact):
    with patch(SEND, return_value="pm-1"):
        account, _ = services.invite(contact, actor=UserFactory())
    return account


def test_update_phone_view_requires_staff_login_and_post(client, contact):
    url = reverse("planner_update_phone", args=[contact.pk])
    response = client.post(url)
    assert response.status_code == 302 and "/login/" in response.url
    client.force_login(UserFactory())
    assert client.get(url).status_code == 405


def test_update_phone_view_copies_the_contact_number(staff, contact):
    account = _registered(contact)
    contact.phone = "(571) 555-0199"
    contact.save()
    response = staff.post(reverse("planner_update_phone", args=[contact.pk]))
    assert response.status_code == 302
    account.refresh_from_db()
    assert account.phone == "+15715550199"


def test_access_card_shows_last4_and_the_button_only_when_numbers_differ(staff, contact):
    _registered(contact)
    html = _profile(staff, contact)
    assert "Codes go to the number ending in 0142" in html
    assert "Use contact" not in html
    contact.phone = "(571) 555-0199"
    contact.save()
    html = _profile(staff, contact)
    assert "Use contact&#x27;s number" in html or "Use contact's number" in html
    assert "Change sign-in number" in html
    assert "ending in 0199" in html
    assert reverse("planner_update_phone", args=[contact.pk]) in html


def test_update_sign_in_phone_audits_under_the_contact_profile(contact):
    agent = UserFactory()
    account = _registered(contact)
    contact.phone = "(571) 555-0199"
    contact.save()
    services.update_sign_in_phone(account, actor=agent)
    entry = AuditEntry.objects.filter(profile=f"contact:{contact.pk}", actor=agent).latest("pk")
    assert "phone" in entry.changes
