import pytest

from apps.leads.factories import ServiceTypeFactory
from apps.messaging import listing
from apps.messaging.factories import MessageRowFactory
from apps.messaging.models import MessageTemplate

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _no_migrated_rows(db):
    MessageTemplate.objects.all().delete()


def _row(**kw):
    kw.setdefault("recipients", ["booking_contact"])
    kw.setdefault("send_sms", True)
    kw.setdefault("send_email", False)
    return MessageRowFactory(**kw)


def test_summary_dated_trigger_with_roles_and_service_type():
    wedding = ServiceTypeFactory(name="Wedding")
    row = _row(
        trigger="pickup_dispatched",
        timing_amount=3,
        timing_unit="hours",
        timing_direction="before",
        recipients=["passengers", "role:day_of_coordinator"],
    )
    row.service_types.add(wedding)
    assert listing.summary(row) == (
        "3 hours before pickup, once dispatched · Text · Passengers, Day-of coordinator · Wedding"
    )


def test_summary_event_right_after_both_channels():
    row = _row(trigger="quote_sent", send_email=True)
    assert listing.summary(row) == "Right after quote sent · Text and Email · Booking contact"


def test_summary_event_with_delay_and_singular_unit():
    row = _row(
        trigger="quote_sent", timing_amount=1, timing_unit="days", send_sms=False, send_email=True
    )
    assert listing.summary(row) == "1 day after quote sent · Email · Booking contact"


def test_summary_staff_click():
    row = _row(trigger="send_quote", send_sms=False, send_email=True)
    assert listing.summary(row) == "Staff click · Email · Booking contact"


def test_summary_staff_audience_names_emails():
    row = _row(
        trigger="deposit_report",
        audience="staff",
        recipients=[],
        staff_emails=["a@x.com", "b@x.com"],
        send_sms=False,
        send_email=True,
    )
    assert listing.summary(row).endswith("Email · 2 staff emails")


def test_problems_none_when_healthy():
    assert listing.problems(_row()) == []


def test_problem_no_recipients():
    assert listing.problems(_row(recipients=[])) == ["No recipients ticked, so it won't send"]


def test_problem_staff_without_emails():
    row = _row(audience="staff", trigger="deposit_report", recipients=[], staff_emails=[])
    assert "No recipients ticked, so it won't send" in listing.problems(row)


def test_problem_field_the_trigger_cannot_fill():
    row = _row(trigger="lead_created", sms_body="Your driver is {driver_name}")
    assert "Uses a field this trigger can't fill" in listing.problems(row)


def test_problem_on_under_master_off():
    row = _row(enabled=True)
    assert listing.problems(row, master_on=False) == ["On, but all messaging is off"]
    assert listing.problems(_row(enabled=False), master_on=False) == []


def test_staff_click_row_has_no_master_problem():
    row = _row(trigger="send_quote")
    assert listing.problems(row, master_on=False) == []


def test_sections_fixed_order_problems_first_then_name():
    _row(name="Zed", section="trip_day", trigger="quote_sent")
    _row(name="Alpha", section="trip_day", trigger="quote_sent")
    _row(name="Broken", section="trip_day", trigger="quote_sent", recipients=[])
    _row(name="Lead one", section="leads_quotes")
    sections = listing.build_sections(MessageTemplate.objects.live(), {})
    keys = [s.key for s in sections]
    assert keys == [s.value for s in MessageTemplate.Section if s.value in keys]
    trip = next(s for s in sections if s.key == "trip_day")
    assert [r.name for r in trip.rows] == ["Broken", "Alpha", "Zed"]
    assert trip.attention == 1 and trip.count == 3


def test_empty_sections_shown_without_filters_and_hidden_with():
    _row(name="Lead one", section="leads_quotes")
    all_sections = listing.build_sections(MessageTemplate.objects.live(), {})
    assert len(all_sections) == len(MessageTemplate.Section)
    filtered = listing.build_sections(MessageTemplate.objects.live(), {"q": "lead"})
    assert [s.key for s in filtered] == ["leads_quotes"]


def test_archived_rows_not_listed():
    row = _row(name="Gone")
    MessageTemplate.objects.filter(pk=row.pk).update(archived_at="2026-01-01T00:00:00Z")
    sections = listing.build_sections(MessageTemplate.objects.live(), {})
    assert all(not s.rows for s in sections)


def _names(filters):
    sections = listing.build_sections(MessageTemplate.objects.live(), filters)
    return {r.name for s in sections for r in s.rows}


def test_filters():
    wedding = ServiceTypeFactory(name="Wedding")
    a = _row(name="A", audience="customer", send_sms=True, send_email=False, enabled=True)
    b = _row(
        name="Bravo",
        audience="planner",
        send_sms=False,
        send_email=True,
        enabled=False,
        trigger="quote_sent",
        seeded=True,
    )
    c = _row(name="C", send_sms=True, send_email=True, trigger="quote_sent")
    c.service_types.add(wedding)
    assert _names({"q": "brav"}) == {"Bravo"}
    assert _names({"audience": "planner"}) == {"Bravo"}
    assert _names({"channel": "sms"}) == {"A", "C"}
    assert _names({"channel": "email"}) == {"Bravo", "C"}
    assert _names({"channel": "both"}) == {"C"}
    assert _names({"trigger": "quote_sent"}) == {"Bravo", "C"}
    assert _names({"state": "off"}) == {"Bravo"}
    assert _names({"state": "on"}) == {"A", "C"}
    assert _names({"origin": "seeded"}) == {"Bravo"}
    assert _names({"origin": "added"}) == {"A", "C"}
    assert _names({"service": str(wedding.pk)}) == {"A", "Bravo", "C"}
    other = ServiceTypeFactory(name="Prom")
    assert _names({"service": str(other.pk)}) == {"A", "Bravo"}
    assert a and b


def test_tags_added_and_edited():
    from apps.messaging.defaults import DEFAULTS, seed

    seed()
    key = next(iter(DEFAULTS))
    MessageTemplate.objects.filter(key=key).update(sms_body="Changed {first_name}")
    _row(name="Mine")
    rows = {
        r.name: r
        for s in listing.build_sections(MessageTemplate.objects.live(), {})
        for r in s.rows
    }
    assert rows["Mine"].tag == "Added"
    assert rows[MessageTemplate.objects.get(key=key).name].tag == "Edited"
    untouched = [r for r in rows.values() if r.tag == ""]
    assert untouched
