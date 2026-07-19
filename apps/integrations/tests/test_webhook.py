import json

import pytest

from apps.contacts.factories import ContactFactory
from apps.contacts.models import Contact
from apps.integrations.models import PodiumEvent
from apps.integrations.webhooks import process_podium_webhook
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging.factories import MessageFactory
from apps.messaging.models import Message

pytestmark = pytest.mark.django_db


def _received(uid="m1", phone="+12025550100", body="Need a quote", name="Sarah", cuid="c1"):
    return {
        "eventType": "message.received",
        "data": {
            "uid": uid,
            "body": body,
            "contact": {"uid": cuid, "name": name, "phoneNumber": phone},
            "conversation": {"uid": "conv1", "channel": {"type": "phone", "identifier": phone}},
            "location": {"uid": "loc", "organizationUid": "org"},
        },
    }


def test_received_creates_event_lead_and_inbound_message():
    event = process_podium_webhook(_received())
    assert event.event_type == "message.received"
    assert event.processed is True
    msg = Message.objects.get()
    assert msg.is_inbound is True
    assert msg.body == "Need a quote"
    assert msg.podium_message_uid == "m1"
    assert msg.delivery_status == Message.DeliveryStatus.RECEIVED
    assert Contact.objects.filter(phones__e164="+12025550100").exists()
    assert event.lead == msg.lead


def test_received_is_idempotent_on_message_uid():
    process_podium_webhook(_received(uid="dup"))
    process_podium_webhook(_received(uid="dup"))
    assert Message.objects.filter(podium_message_uid="dup").count() == 1


def test_received_attaches_to_existing_contacts_lead():
    contact = ContactFactory(phone="+12025550199", podium_contact_uid="cX")
    lead = LeadFactory(contact=contact)
    process_podium_webhook(_received(uid="m2", phone="+12025550199", cuid="cX"))
    assert Message.objects.get().lead == lead
    assert Lead.objects.count() == 1


def test_failed_marks_outbound_message_failed():
    msg = MessageFactory(direction=Message.Direction.OUT, podium_message_uid="mf")
    process_podium_webhook(
        {"eventType": "message.failed", "data": {"uid": "mf", "failureReason": "landline"}}
    )
    msg.refresh_from_db()
    assert msg.delivery_status == Message.DeliveryStatus.FAILED
    assert msg.failure_reason == "landline"


def _sent(uid="s1", phone="+12025550100", body="On our way!", contact_uid="c1", conv_uid="conv1"):
    return {
        "eventType": "message.sent",
        "data": {
            "uid": uid,
            "body": body,
            "contact": {"uid": contact_uid, "phoneNumber": phone},
            "conversation": {"uid": conv_uid, "channel": {"type": "phone", "identifier": phone}},
        },
    }


def test_sent_updates_existing_message_by_uid_no_duplicate():
    contact = ContactFactory(phone="+12025550100", podium_contact_uid="c1")
    lead = LeadFactory(contact=contact)
    msg = MessageFactory(
        lead=lead,
        direction=Message.Direction.OUT,
        podium_message_uid="s1",
        delivery_status="",
        sent_at=None,
    )

    event = process_podium_webhook(_sent(uid="s1"))

    msg.refresh_from_db()
    assert msg.delivery_status == Message.DeliveryStatus.SENT
    assert msg.sent_at is not None
    assert Message.objects.filter(podium_message_uid="s1").count() == 1
    assert event.lead == lead


def test_sent_unknown_uid_creates_outbound_message_on_matched_lead():
    contact = ContactFactory(phone="+12025550100", podium_contact_uid="c1")
    lead = LeadFactory(contact=contact)

    event = process_podium_webhook(_sent(uid="new-sent-uid"))

    msg = Message.objects.get(podium_message_uid="new-sent-uid")
    assert msg.direction == Message.Direction.OUT
    assert msg.lead == lead
    assert msg.delivery_status == Message.DeliveryStatus.SENT
    assert event.lead == lead


def test_sent_replay_of_same_event_stays_one_row():
    contact = ContactFactory(phone="+12025550100", podium_contact_uid="c1")
    LeadFactory(contact=contact)

    process_podium_webhook(_sent(uid="dup-sent"))
    process_podium_webhook(_sent(uid="dup-sent"))

    assert Message.objects.filter(podium_message_uid="dup-sent").count() == 1


def test_sent_with_no_matching_lead_is_skipped_no_orphan_contact():
    before = Contact.objects.count()
    event = process_podium_webhook(_sent(uid="orphan", phone="+12025550111", contact_uid="unknown"))

    assert not Message.objects.filter(podium_message_uid="orphan").exists()
    assert Contact.objects.count() == before
    assert event.lead is None


def _received_email(
    uid="e1", email="sarah@example.com", body="Need a quote", name="Sarah", cuid="cE1"
):
    return {
        "eventType": "message.received",
        "data": {
            "uid": uid,
            "body": body,
            "contact": {"uid": cuid, "name": name},
            "conversation": {"uid": "convE", "channel": {"type": "email", "identifier": email}},
            "location": {"uid": "loc", "organizationUid": "org"},
        },
    }


def test_received_email_dedupes_on_email_identifier():
    """Two inbound emails from the same address must resolve to ONE contact and ONE
    lead — the identifier is an email address, not a phone number, so it must be
    routed to `match_or_create(email=...)` rather than `phone=...` (which would
    silently fail to normalize and create a fresh contact every time)."""
    process_podium_webhook(_received_email(uid="e1", cuid="cE1"))
    process_podium_webhook(_received_email(uid="e2", cuid="cE2"))

    assert Contact.objects.filter(email="sarah@example.com").count() == 1
    assert Lead.objects.count() == 1
    assert Message.objects.count() == 2


def test_received_email_contact_has_no_phone_row():
    process_podium_webhook(_received_email(uid="e3", cuid="cE3"))
    contact = Contact.objects.get(email="sarah@example.com")
    assert contact.phones.count() == 0


def _sent_email(uid="se1", email="sarah@example.com", contact_uid="cE1", conv_uid="convE"):
    return {
        "eventType": "message.sent",
        "data": {
            "uid": uid,
            "body": "On our way!",
            "contact": {"uid": contact_uid},
            "conversation": {"uid": conv_uid, "channel": {"type": "email", "identifier": email}},
        },
    }


def test_sent_email_matches_existing_contact_by_email_readonly():
    contact = ContactFactory(email="sarah@example.com")
    lead = LeadFactory(contact=contact)

    event = process_podium_webhook(_sent_email(uid="se-new", contact_uid="unknown-uid"))

    msg = Message.objects.get(podium_message_uid="se-new")
    assert msg.lead == lead
    assert event.lead == lead


def test_sent_email_with_no_matching_contact_is_skipped_no_orphan_contact():
    before = Contact.objects.count()
    event = process_podium_webhook(
        _sent_email(uid="orphan-email", email="nobody@example.com", contact_uid="unknown")
    )

    assert not Message.objects.filter(podium_message_uid="orphan-email").exists()
    assert Contact.objects.count() == before
    assert event.lead is None


def test_webhook_view_accepts_post(client, settings):
    settings.PODIUM_WEBHOOK_SECRET = ""
    resp = client.post(
        "/webhooks/podium/",
        data=json.dumps(_received(uid="v1")),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert PodiumEvent.objects.filter(event_type="message.received").exists()


def test_webhook_view_rejects_bad_json(client, settings):
    settings.PODIUM_WEBHOOK_SECRET = ""
    resp = client.post("/webhooks/podium/", data="not-json", content_type="application/json")
    assert resp.status_code == 400
