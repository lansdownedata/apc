"""Repair pass for messages ingested before metadata.eventType was read correctly.

Every event fell through to the "message.received" default, so agent replies sent
from the Podium app were stored inbound. The raw payloads survive on PodiumEvent,
so direction and sender can be re-derived rather than re-fetched.
"""

from unittest.mock import patch

import pytest
from django.core.management import call_command

from apps.contacts.factories import ContactFactory
from apps.integrations import podium
from apps.integrations.models import PodiumEvent
from apps.leads.factories import LeadFactory
from apps.messaging.factories import MessageFactory
from apps.messaging.models import Message

pytestmark = pytest.mark.django_db


def _event(event_type, msg_uid, sender_uid=None, contact_name="Mark Shaltanis"):
    return PodiumEvent.objects.create(
        event_type="message.received",  # what the buggy code stored
        payload={
            "data": {
                "uid": msg_uid,
                "body": "b",
                "senderUid": sender_uid,
                "sender": {"uid": sender_uid},
                "contact": {"uid": "c1", "name": contact_name},
                "conversation": {
                    "uid": "conv1",
                    "channel": {"type": "phone", "identifier": "+17035736008"},
                },
            },
            "metadata": {"eventUid": "e-" + msg_uid, "eventType": event_type},
        },
    )


@pytest.fixture
def lead():
    return LeadFactory(contact=ContactFactory(phone="+17035736008", podium_contact_uid="c1"))


def test_repair_flips_misfiled_outbound_to_out(lead):
    msg = MessageFactory(lead=lead, direction=Message.Direction.IN, podium_message_uid="a1")
    _event("message.sent", "a1", sender_uid="u-tarrick")

    with patch.object(podium, "user_name_map", return_value={"u-tarrick": "Tarrick Ghannam"}):
        call_command("podium_repair_directions")

    msg.refresh_from_db()
    assert msg.direction == Message.Direction.OUT
    assert msg.delivery_status == Message.DeliveryStatus.SENT
    assert msg.podium_sender_uid == "u-tarrick"
    assert msg.sender_name == "Tarrick Ghannam"


def test_repair_leaves_genuine_inbound_alone(lead):
    msg = MessageFactory(lead=lead, direction=Message.Direction.IN, podium_message_uid="b1")
    _event("message.received", "b1")

    with patch.object(podium, "user_name_map", return_value={}):
        call_command("podium_repair_directions")

    msg.refresh_from_db()
    assert msg.direction == Message.Direction.IN
    assert msg.sender_name == "Mark Shaltanis"


def test_repair_corrects_the_stored_event_type(lead):
    MessageFactory(lead=lead, direction=Message.Direction.IN, podium_message_uid="c1msg")
    event = _event("message.sent", "c1msg", sender_uid="u-tarrick")

    with patch.object(podium, "user_name_map", return_value={}):
        call_command("podium_repair_directions")

    event.refresh_from_db()
    assert event.event_type == "message.sent"


def test_repair_dry_run_changes_nothing(lead):
    msg = MessageFactory(lead=lead, direction=Message.Direction.IN, podium_message_uid="d1")
    _event("message.sent", "d1", sender_uid="u-tarrick")

    with patch.object(podium, "user_name_map", return_value={"u-tarrick": "Tarrick Ghannam"}):
        call_command("podium_repair_directions", "--dry-run")

    msg.refresh_from_db()
    assert msg.direction == Message.Direction.IN
    assert msg.sender_name == ""


def test_repair_is_idempotent(lead):
    msg = MessageFactory(lead=lead, direction=Message.Direction.IN, podium_message_uid="e1")
    _event("message.sent", "e1", sender_uid="u-tarrick")

    with patch.object(podium, "user_name_map", return_value={"u-tarrick": "Tarrick Ghannam"}):
        call_command("podium_repair_directions")
        call_command("podium_repair_directions")

    msg.refresh_from_db()
    assert msg.direction == Message.Direction.OUT
    assert Message.objects.filter(podium_message_uid="e1").count() == 1


def test_repair_skips_events_with_no_matching_message(lead):
    _event("message.sent", "ghost", sender_uid="u-tarrick")

    with patch.object(podium, "user_name_map", return_value={}):
        call_command("podium_repair_directions")

    assert not Message.objects.filter(podium_message_uid="ghost").exists()
