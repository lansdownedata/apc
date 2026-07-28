"""Qualifying actions on a conversation — archive, unarchive, and create-lead."""

import pytest
from django.urls import reverse

from apps.contacts.factories import ContactFactory
from apps.leads.models import Lead
from apps.messaging.factories import ConversationFactory, MessageFactory
from apps.messaging.models import Conversation, TouchPoint

pytestmark = pytest.mark.django_db


@pytest.fixture
def agent(django_user_model):
    return django_user_model.objects.create_user(username="agent", password="pw")


def test_archive_marks_the_conversation_and_records_who(client, agent):
    convo = ConversationFactory()
    client.force_login(agent)

    resp = client.post(reverse("conversation_archive", args=[convo.pk]))

    assert resp.status_code == 302
    convo.refresh_from_db()
    assert convo.status == Conversation.Status.ARCHIVED
    assert convo.archived_at is not None
    assert convo.archived_by == agent


def test_unarchive_clears_the_archive_stamps(client, agent):
    convo = ConversationFactory(status=Conversation.Status.ARCHIVED, archived_by=agent)
    client.force_login(agent)

    client.post(reverse("conversation_unarchive", args=[convo.pk]))

    convo.refresh_from_db()
    assert convo.status == Conversation.Status.OPEN
    assert convo.archived_at is None
    assert convo.archived_by is None


def test_archive_requires_post(client, agent):
    convo = ConversationFactory()
    client.force_login(agent)
    assert client.get(reverse("conversation_archive", args=[convo.pk])).status_code == 405


def test_archive_requires_login(client):
    convo = ConversationFactory()
    resp = client.post(reverse("conversation_archive", args=[convo.pk]))
    assert resp.status_code == 302
    assert "/login" in resp.url


def test_create_lead_requires_login(client):
    convo = ConversationFactory()
    resp = client.post(reverse("conversation_create_lead", args=[convo.pk]))
    assert resp.status_code == 302
    assert "/login" in resp.url


def test_create_lead_makes_a_new_lead_on_the_contact(client, agent):
    contact = ContactFactory()
    convo = ConversationFactory(contact=contact)
    MessageFactory(conversation=convo, lead=None)
    client.force_login(agent)

    resp = client.post(reverse("conversation_create_lead", args=[convo.pk]))

    lead = Lead.objects.get()
    assert lead.contact == contact
    assert lead.status == Lead.Status.NEW
    assert lead.assigned_agent == agent
    assert resp.status_code == 302
    assert resp.url == reverse("lead_detail", args=[lead.pk])


def test_create_lead_schedules_the_welcome_touchpoints(client, agent):
    convo = ConversationFactory()
    client.force_login(agent)

    client.post(reverse("conversation_create_lead", args=[convo.pk]))

    kinds = set(TouchPoint.objects.values_list("kind", flat=True))
    assert kinds == {TouchPoint.Kind.TP1_WELCOME, TouchPoint.Kind.TP2_LEAD_FOLLOWUP}


def test_create_lead_twice_yields_two_leads_on_one_conversation(client, agent):
    """Repeatable on purpose — this is how one conversation yields several quotes."""
    contact = ContactFactory()
    convo = ConversationFactory(contact=contact)
    client.force_login(agent)

    client.post(reverse("conversation_create_lead", args=[convo.pk]))
    client.post(reverse("conversation_create_lead", args=[convo.pk]))

    assert Lead.objects.filter(contact=contact).count() == 2


def test_create_lead_works_on_an_archived_conversation(client, agent):
    """An agent may archive, then reconsider — no status guard."""
    convo = ConversationFactory(status=Conversation.Status.ARCHIVED)
    client.force_login(agent)

    client.post(reverse("conversation_create_lead", args=[convo.pk]))

    assert Lead.objects.count() == 1
