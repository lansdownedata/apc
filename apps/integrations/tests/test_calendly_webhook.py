"""invitee.created → Contact + Lead; invitee.canceled → a note, never a deletion.

Payload shapes follow Calendly's documented invitee resource. The raw delivery is
always archived on CalendlyEvent.payload, so anything the docs got wrong can be
refined against real traffic without losing the evidence.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.contacts.models import Contact
from apps.core.choices import Channel
from apps.integrations.models import CalendlyEvent
from apps.integrations.webhooks import process_calendly_webhook
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging.models import TouchPoint

pytestmark = pytest.mark.django_db

INVITEE_URI = "https://api.calendly.com/scheduled_events/ev1/invitees/inv1"
EVENT_TYPE_URI = "https://api.calendly.com/event_types/EAFTEGE2V6TLJSZT"


def _created(email="sarah@example.com", name="Sarah Smith", uri=INVITEE_URI, **overrides):
    scheduled = {
        "uri": "https://api.calendly.com/scheduled_events/ev1",
        "name": "Quick Chat",
        "start_time": "2026-09-08T18:30:00.000000Z",
        "end_time": "2026-09-08T18:45:00.000000Z",
        "status": "active",
        "event_type": EVENT_TYPE_URI,
    }
    scheduled.update(overrides.pop("scheduled_event", {}))
    payload = {
        "event": "invitee.created",
        "payload": {
            "uri": uri,
            "name": name,
            "email": email,
            "timezone": "America/New_York",
            "text_reminder_number": "+15551230000",
            "questions_and_answers": [],
            "tracking": {},
            "cancel_url": "https://calendly.com/cancellations/inv1",
            "reschedule_url": "https://calendly.com/reschedulings/inv1",
            "rescheduled": False,
            "old_invitee": None,
            "scheduled_event": scheduled,
        },
    }
    payload["payload"].update(overrides)
    return payload


def _canceled(uri=INVITEE_URI, **overrides):
    payload = {"event": "invitee.canceled", "payload": {"uri": uri, "rescheduled": False}}
    payload["payload"].update(overrides)
    return payload


# --- the happy path ------------------------------------------------------------


def test_creates_contact_and_lead():
    event = process_calendly_webhook(_created())

    assert event.event_type == "invitee.created"
    assert event.processed is True

    contact = Contact.objects.get(email="sarah@example.com")
    assert contact.name == "Sarah Smith"

    lead = Lead.objects.get()
    assert lead.contact == contact
    assert lead.status == Lead.Status.NEW
    assert lead.channel == Channel.WEBSITE
    assert event.lead == lead


def test_note_carries_the_call_time_in_the_company_timezone():
    """The office reads the pipeline in ET. A raw UTC string in notes is a bug."""
    process_calendly_webhook(_created())
    notes = Lead.objects.get().notes
    assert "Quick Chat" in notes
    assert "2:30 PM EDT" in notes


def test_note_carries_utm_attribution_when_present():
    """Which ad drove the call is the whole point of tracking it — and it is only
    ever in the payload, never anywhere a person would look."""
    process_calendly_webhook(
        _created(
            tracking={
                "utm_source": "google",
                "utm_medium": "cpc",
                "utm_campaign": "spring-charter",
            }
        )
    )
    assert "google / cpc / spring-charter" in Lead.objects.get().notes


def test_note_omits_the_attribution_line_when_there_is_no_tracking():
    process_calendly_webhook(_created())
    assert "Source:" not in Lead.objects.get().notes


def test_raw_payload_is_archived_on_the_lead():
    process_calendly_webhook(_created())
    payload = Lead.objects.get().intake_payload
    assert payload["uri"] == INVITEE_URI
    # cancel/reschedule URLs are not in notes, but must stay recoverable.
    assert payload["reschedule_url"] == "https://calendly.com/reschedulings/inv1"


def test_redelivery_creates_exactly_one_lead():
    """Calendly retries until it sees a timely 2xx."""
    process_calendly_webhook(_created())
    process_calendly_webhook(_created())
    assert Lead.objects.count() == 1
    assert CalendlyEvent.objects.count() == 1


def test_existing_contact_is_reused_not_duplicated():
    existing = ContactFactory(email="sarah@example.com")
    process_calendly_webhook(_created())
    assert Contact.objects.filter(email="sarah@example.com").count() == 1
    assert Lead.objects.get().contact == existing


def test_no_touchpoints_are_scheduled():
    """TP1/TP2 copy is website-worded ("thank you for visiting our website") and
    reads badly to someone who just booked a call. Deliberate."""
    process_calendly_webhook(_created())
    assert TouchPoint.objects.count() == 0


# --- messy but valid invitees ---------------------------------------------------


def test_phone_falls_back_to_a_questions_and_answers_row():
    process_calendly_webhook(
        _created(
            text_reminder_number="",
            questions_and_answers=[
                {
                    "question": "What's the best phone number?",
                    "answer": "202-555-0142",
                    "position": 0,
                }
            ],
        )
    )
    assert Lead.objects.get().contact.phone


def test_email_only_invitee_still_becomes_a_lead():
    """A Calendly booking often carries an email and nothing else."""
    process_calendly_webhook(_created(text_reminder_number="", questions_and_answers=[]))
    assert Lead.objects.count() == 1


def test_missing_start_time_still_creates_the_lead():
    process_calendly_webhook(_created(scheduled_event={"start_time": ""}))
    assert Lead.objects.count() == 1


def test_nameless_invitee_gets_a_placeholder_not_a_blank_contact():
    process_calendly_webhook(_created(name=""))
    assert Contact.objects.get().name


# --- things that must not become leads -------------------------------------------


def test_unknown_event_type_is_ignored():
    assert process_calendly_webhook({"event": "routing_form_submission.created"}) is None
    assert Lead.objects.count() == 0


def test_payload_with_no_invitee_uri_is_ignored():
    assert process_calendly_webhook({"event": "invitee.created", "payload": {}}) is None
    assert Lead.objects.count() == 0


def test_bookings_for_other_event_types_are_ignored(settings):
    """A subscription is account-wide: it fires for EVERY meeting booked on the
    account. Without this filter, a vendor call or an internal 1:1 lands in the
    sales pipeline."""
    settings.CALENDLY_EVENT_TYPE_URI = EVENT_TYPE_URI
    other = _created(scheduled_event={"event_type": "https://api.calendly.com/event_types/OTHER"})
    assert process_calendly_webhook(other) is None
    assert Lead.objects.count() == 0


def test_the_configured_event_type_still_gets_through(settings):
    settings.CALENDLY_EVENT_TYPE_URI = EVENT_TYPE_URI
    assert process_calendly_webhook(_created()) is not None
    assert Lead.objects.count() == 1


def test_blank_event_type_setting_accepts_everything(settings):
    settings.CALENDLY_EVENT_TYPE_URI = ""
    other = _created(scheduled_event={"event_type": "https://api.calendly.com/event_types/OTHER"})
    assert process_calendly_webhook(other) is not None


# --- attaching to an existing lead (option A) -------------------------------------


def test_attaches_to_the_single_open_lead_instead_of_duplicating():
    """Someone booking a call while a quote sits unanswered is almost always calling
    about that quote. A Calendly booking carries no trip date, so lead STATE is the
    only signal available."""
    contact = ContactFactory(email="sarah@example.com")
    existing = LeadFactory(contact=contact, status=Lead.Status.QUOTED)

    event = process_calendly_webhook(_created())

    assert Lead.objects.count() == 1
    assert event.lead == existing
    existing.refresh_from_db()
    assert "Quick Chat" in existing.notes


def test_two_open_leads_is_ambiguous_so_a_new_one_is_created():
    """Guessing wrong buries the note on the wrong quote — worse than an extra row."""
    contact = ContactFactory(email="sarah@example.com")
    LeadFactory(contact=contact, status=Lead.Status.NEW)
    LeadFactory(contact=contact, status=Lead.Status.QUOTED)

    process_calendly_webhook(_created())
    assert Lead.objects.count() == 3


def test_a_booked_or_lost_lead_does_not_absorb_a_new_enquiry():
    contact = ContactFactory(email="sarah@example.com")
    LeadFactory(contact=contact, status=Lead.Status.BOOKED)
    LeadFactory(contact=contact, status=Lead.Status.LOST)

    process_calendly_webhook(_created())
    assert Lead.objects.filter(status=Lead.Status.NEW).count() == 1


def test_a_stale_open_lead_does_not_absorb_a_new_enquiry():
    contact = ContactFactory(email="sarah@example.com")
    stale = LeadFactory(contact=contact, status=Lead.Status.QUOTED)
    Lead.objects.filter(pk=stale.pk).update(updated_at=timezone.now() - timedelta(days=120))

    process_calendly_webhook(_created())
    assert Lead.objects.count() == 2


# --- cancellation and rescheduling -------------------------------------------------


def test_cancellation_annotates_the_lead_without_losing_it():
    process_calendly_webhook(_created())
    lead = Lead.objects.get()

    event = process_calendly_webhook(_canceled())

    lead.refresh_from_db()
    assert "canceled" in lead.notes.lower()
    assert lead.status == Lead.Status.NEW
    assert event.lead == lead
    assert Lead.objects.count() == 1


def test_cancellation_for_an_unknown_invitee_is_a_no_op():
    event = process_calendly_webhook(_canceled(uri="https://api.calendly.com/x/y"))
    assert event.lead is None
    assert Lead.objects.count() == 0


def test_a_reschedule_does_not_read_as_a_cancellation():
    """Rescheduling fires invitee.canceled (rescheduled=true) AND invitee.created.
    Treating the first as a plain cancellation writes a note that is simply false."""
    process_calendly_webhook(_created())
    lead = Lead.objects.get()

    process_calendly_webhook(_canceled(rescheduled=True))

    lead.refresh_from_db()
    assert "canceled" not in lead.notes.lower()


def test_a_reschedule_updates_the_original_lead_rather_than_making_a_second():
    process_calendly_webhook(_created())
    lead = Lead.objects.get()

    process_calendly_webhook(_canceled(rescheduled=True))
    moved = process_calendly_webhook(
        _created(
            uri="https://api.calendly.com/scheduled_events/ev2/invitees/inv2",
            old_invitee=INVITEE_URI,
            scheduled_event={
                "uri": "https://api.calendly.com/scheduled_events/ev2",
                "start_time": "2026-09-10T19:00:00.000000Z",
            },
        )
    )

    assert Lead.objects.count() == 1
    assert moved.lead == lead
    lead.refresh_from_db()
    assert "3:00 PM EDT" in lead.notes


def test_a_reschedule_whose_original_is_unknown_still_creates_a_lead():
    """Never drop a real booking just because we cannot correlate it."""
    process_calendly_webhook(
        _created(uri="https://api.calendly.com/x/y", old_invitee="https://api.calendly.com/gone")
    )
    assert Lead.objects.count() == 1
