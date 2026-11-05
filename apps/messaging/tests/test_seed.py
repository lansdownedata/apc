from datetime import timedelta

import pytest

from apps.leads.factories import LeadFactory, ServiceTypeFactory
from apps.leads.models import ServiceType
from apps.messaging import defaults
from apps.messaging.models import MessageTemplate, TouchPoint
from apps.messaging.touchpoint_templates import TEMPLATES, THANK_YOU_WEDDING
from apps.public.services import WEDDING_SERVICE_NAME

pytestmark = pytest.mark.django_db

BOOKING = ["booking_contact"]
DRIVER_PEOPLE = ["booking_contact", "role:day_of_coordinator", "role:transportation"]

# key: (trigger, (amount, unit, direction), section, audience, recipients)
TABLE = {
    "tp1_welcome": ("lead_created", (30, "minutes", "after"), "leads_quotes", "customer", BOOKING),
    "tp2_lead_followup": (
        "lead_created",
        (2, "hours", "after"),
        "leads_quotes",
        "customer",
        BOOKING,
    ),
    "tp3_quote_sent_sms": (
        "quote_sent",
        (3, "minutes", "after"),
        "leads_quotes",
        "customer",
        BOOKING,
    ),
    "tp4_viewed_sms": (
        "quote_viewed",
        (20, "minutes", "after"),
        "leads_quotes",
        "customer",
        BOOKING,
    ),
    "tp5_viewed_email": (
        "quote_viewed",
        (2, "hours", "after"),
        "leads_quotes",
        "customer",
        BOOKING,
    ),
    "tp6_quote_followup": (
        "quote_sent",
        (24, "hours", "after"),
        "leads_quotes",
        "customer",
        BOOKING,
    ),
    "tp7_expiring": ("quote_expiry", (24, "hours", "before"), "leads_quotes", "customer", BOOKING),
    "tp8_expired": ("quote_expiry", (24, "hours", "after"), "leads_quotes", "customer", BOOKING),
    "payment_reminder": (
        "balance_due",
        (72, "hours", "before"),
        "booking_payments",
        "customer",
        BOOKING,
    ),
    "order_confirmed": (
        "order_confirmed",
        (0, "minutes", "after"),
        "booking_payments",
        "customer",
        BOOKING,
    ),
    "order_cancelled": (
        "order_cancelled",
        (0, "minutes", "after"),
        "booking_payments",
        "customer",
        BOOKING,
    ),
    "order_auth_expired": (
        "deposit_hold_released",
        (0, "minutes", "after"),
        "booking_payments",
        "customer",
        BOOKING,
    ),
    "wed_final_details": (
        "pickup",
        (7, "days", "before"),
        "trip_day",
        "customer",
        ["booking_contact", "role:couple", "role:lead_planner"],
    ),
    "trip_confirm_customer": ("pickup", (72, "hours", "before"), "trip_day", "customer", BOOKING),
    "trip_confirm_customer_2": ("pickup", (48, "hours", "before"), "trip_day", "customer", BOOKING),
    "trip_confirm_affiliate": (
        "pickup",
        (48, "hours", "before"),
        "affiliates",
        "affiliate",
        ["company"],
    ),
    "driver_released": (
        "pickup_dispatched",
        (12, "hours", "before"),
        "trip_day",
        "customer",
        DRIVER_PEOPLE,
    ),
    "driver_updated": (
        "driver_changed",
        (0, "minutes", "after"),
        "trip_day",
        "customer",
        DRIVER_PEOPLE,
    ),
    "status_dispatched": (
        "status_dispatched",
        (0, "minutes", "after"),
        "trip_day",
        "customer",
        BOOKING,
    ),
    "status_on_the_way": (
        "status_on_the_way",
        (0, "minutes", "after"),
        "trip_day",
        "customer",
        BOOKING,
    ),
    "status_arrived": (
        "status_arrived",
        (0, "minutes", "after"),
        "trip_day",
        "customer",
        BOOKING,
    ),
    "thank_you": ("all_trips_done", (2, "hours", "after"), "after_trip", "customer", BOOKING),
    "thank_you_wedding": (
        "all_trips_done",
        (2, "hours", "after"),
        "after_trip",
        "customer",
        BOOKING,
    ),
    "review_request": ("all_trips_done", (1, "days", "after"), "after_trip", "customer", BOOKING),
}
WEDDING_ONLY = {"wed_final_details", "thank_you_wedding"}
OFF = {"status_dispatched", "status_on_the_way", "status_arrived"}


def _template(key):
    return THANK_YOU_WEDDING if key == "thank_you_wedding" else TEMPLATES[key]


def _span(amount, unit, direction):
    span = timedelta(**{unit: amount})
    return -span if direction == "before" else span


# --- the code defaults -------------------------------------------------------------------


def test_defaults_cover_every_kind_and_the_wedding_thank_you():
    assert set(defaults.DEFAULTS) == set(TouchPoint.Kind.values) | {"thank_you_wedding"}
    assert set(defaults.DEFAULTS) == set(TABLE)


@pytest.mark.parametrize("key", sorted(TABLE))
def test_default_matches_the_routing_table(key):
    trigger, timing, section, audience, recipients = TABLE[key]
    d = defaults.DEFAULTS[key]
    assert d.key == key
    assert d.trigger == trigger
    assert (d.timing_amount, d.timing_unit, d.timing_direction) == timing
    assert d.section == section
    assert d.audience == audience
    assert list(d.recipients) == recipients
    assert d.staff_emails == ()
    assert d.enabled is (key not in OFF)
    assert d.service_type_names == ((defaults.WEDDING_SERVICE_NAME,) if key in WEDDING_ONLY else ())


@pytest.mark.parametrize("key", sorted(TABLE))
def test_default_wording_and_timing_come_from_the_templates(key):
    base, d = _template(key), defaults.DEFAULTS[key]
    # The very same string objects: the client copy lives once, in touchpoint_templates.
    assert d.subject is base.subject
    assert d.email_body is base.email_body
    assert d.sms_body is base.sms_body
    assert d.send_sms is ("sms" in base.channels)
    assert d.send_email is ("email" in base.channels)
    assert _span(d.timing_amount, d.timing_unit, d.timing_direction) == base.offset


# Spec B's Messages page labels (`overrides.MESSAGES`, removed with that page).
SPEC_B_LABELS = {
    "tp1_welcome": "Welcome message",
    "tp2_lead_followup": "Lead follow-up",
    "tp3_quote_sent_sms": "Quote sent",
    "tp4_viewed_sms": "Quote viewed (text)",
    "tp5_viewed_email": "Quote viewed (email)",
    "tp6_quote_followup": "Quote follow-up",
    "tp7_expiring": "Quote expiring",
    "tp8_expired": "Quote expired",
    "order_confirmed": "Order confirmed",
    "order_cancelled": "Order cancelled",
    "order_auth_expired": "Deposit hold released",
    "payment_reminder": "Payment reminder",
    "wed_final_details": "Wedding details request",
    "trip_confirm_customer": "Trip confirmation",
    "trip_confirm_customer_2": "Trip confirmation — second notice",
    "trip_confirm_affiliate": "Affiliate trip confirmation",
    "driver_released": "Driver details",
    "driver_updated": "Driver details updated",
    "status_on_the_way": "On the way",
    "status_arrived": "Arrived",
    "thank_you": "Thank-you",
    "thank_you_wedding": "Thank-you (wedding)",
    "review_request": "Review request",
}


def test_names_are_todays_labels():
    for kind, label in SPEC_B_LABELS.items():
        assert defaults.DEFAULTS[kind].name == label
    # Not in today's catalogue; named like its siblings ("On the way", "Arrived").
    assert defaults.DEFAULTS["status_dispatched"].name == "Dispatched"


def test_wedding_service_name_matches_the_public_site():
    assert defaults.WEDDING_SERVICE_NAME == WEDDING_SERVICE_NAME


# --- the migration ran ---------------------------------------------------------------------


def test_the_migration_seeded_every_row():
    rows = {row.key: row for row in MessageTemplate.objects.all()}
    assert set(rows) >= set(defaults.DEFAULTS)
    for key, (trigger, timing, section, audience, recipients) in TABLE.items():
        row = rows[key]
        assert row.seeded is True
        assert row.name == defaults.DEFAULTS[key].name
        assert row.trigger == trigger
        assert (row.timing_amount, row.timing_unit, row.timing_direction) == timing
        assert (row.section, row.audience, row.recipients) == (section, audience, recipients)
        assert row.sms_body == _template(key).sms_body
        assert row.enabled is (key not in OFF)
    wedding = rows["wed_final_details"].service_types.get()
    assert wedding.name.lower() == WEDDING_SERVICE_NAME.lower()
    assert list(rows["thank_you_wedding"].service_types.all()) == [wedding]
    assert not rows["trip_confirm_customer"].service_types.exists()


def test_seed_twice_creates_no_duplicates():
    before = MessageTemplate.objects.count()
    assert defaults.seed() == 0
    assert defaults.seed() == 0
    assert MessageTemplate.objects.count() == before
    assert ServiceType.objects.filter(name__iexact=WEDDING_SERVICE_NAME).count() == 1


def test_seed_never_overwrites_a_seeded_row():
    MessageTemplate.objects.filter(key="tp1_welcome").update(sms_body="Edited {first_name}")
    defaults.seed()
    assert MessageTemplate.objects.get(key="tp1_welcome").sms_body == "Edited {first_name}"


# --- seeding over spec B's saved rows ------------------------------------------------------


def _before_seeding(**saved):
    """The table as spec B left it: only the owner's edits, nothing seeded."""
    MessageTemplate.objects.all().delete()
    for key, values in saved.items():
        MessageTemplate.objects.create(key=key, **{"timing_unit": "", **values})


def test_spec_b_edits_survive_into_the_seeded_row():
    _before_seeding(tp1_welcome={"sms_body": "Hi {first_name}", "enabled": False})
    assert defaults.seed() == len(defaults.DEFAULTS)
    row = MessageTemplate.objects.get(key="tp1_welcome")
    base = TEMPLATES["tp1_welcome"]
    assert row.sms_body == "Hi {first_name}"
    assert row.enabled is False
    assert (row.subject, row.email_body) == (base.subject, base.email_body)
    assert (row.timing_amount, row.timing_unit) == (30, "minutes")
    assert row.seeded is True
    assert row.trigger == "lead_created"
    assert row.name == "Welcome message"
    assert MessageTemplate.objects.filter(key="tp1_welcome").count() == 1


def test_spec_b_timing_wins_when_set():
    _before_seeding(tp7_expiring={"timing_amount": 2, "timing_unit": "days"})
    defaults.seed()
    row = MessageTemplate.objects.get(key="tp7_expiring")
    assert (row.timing_amount, row.timing_unit, row.timing_direction) == (2, "days", "before")


def test_a_stray_timing_on_a_triggered_message_is_dropped():
    # Spec B never applied a timing to a message that sends when it happens.
    _before_seeding(order_confirmed={"timing_amount": 5, "timing_unit": "hours"})
    defaults.seed()
    row = MessageTemplate.objects.get(key="order_confirmed")
    assert (row.timing_amount, row.timing_unit) == (0, "minutes")


def test_rows_without_spec_b_data_take_the_default_switch():
    _before_seeding()
    defaults.seed()
    assert MessageTemplate.objects.get(key="status_on_the_way").enabled is False
    assert MessageTemplate.objects.get(key="status_dispatched").enabled is False
    assert MessageTemplate.objects.get(key="tp1_welcome").enabled is True


def test_a_status_message_turned_on_in_spec_b_stays_on():
    _before_seeding(status_arrived={"enabled": True})
    defaults.seed()
    assert MessageTemplate.objects.get(key="status_arrived").enabled is True


@pytest.mark.parametrize(
    "child, parent",
    [("trip_confirm_customer_2", "trip_confirm_customer"), ("thank_you_wedding", "thank_you")],
)
def test_shared_switch_becomes_the_rows_own(child, parent):
    # Spec B saved the parent off and never stored a switch for the child.
    _before_seeding(**{parent: {"enabled": False}, child: {"sms_body": "x {first_name}"}})
    defaults.seed()
    assert MessageTemplate.objects.get(key=child).enabled is False
    assert MessageTemplate.objects.get(key=parent).enabled is False


def test_shared_switch_defaults_to_the_parents_default():
    _before_seeding()
    defaults.seed()
    assert MessageTemplate.objects.get(key="trip_confirm_customer_2").enabled is True
    assert MessageTemplate.objects.get(key="thank_you_wedding").enabled is True


def test_wedding_thank_you_takes_the_thank_you_timing():
    # Today the wedding thank-you goes out on the Thank-you row's timing.
    _before_seeding(thank_you={"timing_amount": 4, "timing_unit": "hours"})
    defaults.seed()
    row = MessageTemplate.objects.get(key="thank_you_wedding")
    assert (row.timing_amount, row.timing_unit, row.timing_direction) == (4, "hours", "after")


def test_seed_creates_the_wedding_service_type_when_missing():
    _before_seeding()
    ServiceType.objects.filter(name__iexact=WEDDING_SERVICE_NAME).delete()
    defaults.seed()
    wedding = ServiceType.objects.get(name=WEDDING_SERVICE_NAME)
    assert wedding.active is False
    assert list(MessageTemplate.objects.get(key="wed_final_details").service_types.all()) == [
        wedding
    ]


def test_seed_matches_the_wedding_service_type_ignoring_case():
    _before_seeding()
    ServiceType.objects.filter(name__iexact=WEDDING_SERVICE_NAME).delete()
    mine = ServiceTypeFactory(name=WEDDING_SERVICE_NAME.upper())
    defaults.seed()
    assert ServiceType.objects.filter(name__iexact=WEDDING_SERVICE_NAME).count() == 1
    assert MessageTemplate.objects.get(key="thank_you_wedding").service_types.get() == mine


def test_existing_touchpoints_get_their_message():
    lead = LeadFactory()
    welcome = TouchPoint.objects.create(lead=lead, kind="tp1_welcome")
    orphan = TouchPoint.objects.create(lead=lead, kind="no_such_message")
    defaults.seed()
    welcome.refresh_from_db(), orphan.refresh_from_db()
    assert welcome.message == MessageTemplate.objects.get(key="tp1_welcome")
    assert orphan.message is None


# --- reset ---------------------------------------------------------------------------------


def test_reset_copies_the_original_back():
    row = MessageTemplate.objects.get(key="driver_released")
    row.sms_body = "Changed {first_name}"
    row.timing_amount, row.timing_unit = 3, "hours"
    row.trigger = "pickup"
    row.recipients = ["passengers"]
    row.name = "Renamed"
    row.enabled = False
    row.save()
    row.service_types.add(ServiceTypeFactory())

    got = defaults.reset(row)

    got.refresh_from_db()
    d = defaults.DEFAULTS["driver_released"]
    assert got.sms_body == d.sms_body
    assert (got.timing_amount, got.timing_unit) == (12, "hours")
    assert got.trigger == "pickup_dispatched"
    assert got.recipients == DRIVER_PEOPLE
    assert got.name == "Driver details"
    assert not got.service_types.exists()
    assert got.enabled is False  # the switch is not wording; reset leaves it alone


def test_reset_restores_the_wedding_filter():
    row = MessageTemplate.objects.get(key="wed_final_details")
    row.service_types.clear()
    defaults.reset(row)
    assert row.service_types.get().name.lower() == WEDDING_SERVICE_NAME.lower()


# --- the frozen key list of migration 0022 --------------------------------------------------


def _migration_0022():
    from importlib import import_module

    return import_module("apps.messaging.migrations.0022_seed_message_rows")


def test_0022_seeds_a_frozen_list_of_todays_keys():
    keys = _migration_0022().KEYS
    assert isinstance(keys, tuple)
    assert set(keys) == set(TouchPoint.Kind.values) | {"thank_you_wedding"}
    assert len(keys) == len(set(keys)) == 24


def test_seed_with_keys_writes_only_those_rows():
    _before_seeding()
    assert defaults.seed(keys=("tp1_welcome", "thank_you")) == 2
    assert set(MessageTemplate.objects.values_list("key", flat=True)) == {
        "tp1_welcome",
        "thank_you",
    }


def test_seed_with_keys_links_only_those_touchpoints():
    _before_seeding()
    lead = LeadFactory()
    welcome = TouchPoint.objects.create(lead=lead, kind="tp1_welcome")
    thanks = TouchPoint.objects.create(lead=lead, kind="thank_you")
    defaults.seed(keys=("tp1_welcome",))
    welcome.refresh_from_db(), thanks.refresh_from_db()
    assert welcome.message is not None
    assert thanks.message is None


def test_0022_passes_its_frozen_keys_to_seed(monkeypatch):
    calls = []
    monkeypatch.setattr(defaults, "seed", lambda apps=None, keys=None: calls.append(keys) or 0)
    migration = _migration_0022()
    migration.seed_rows(apps="registry", schema_editor=None)
    assert calls == [migration.KEYS]


def _migration_0030():
    from importlib import import_module

    return import_module("apps.messaging.migrations.0030_confirmation_names_every_trip")


def test_0030_moves_untouched_confirmation_text_to_the_trip_count_sentence():
    """APC-123: the seeded confirmation text says "trip" or "trips" from the day's count."""
    from django.apps import apps as registry

    row = MessageTemplate.objects.get(key="trip_confirm_customer")
    row.sms_body = _migration_0030().OLD
    row.save(update_fields=["sms_body"])

    _migration_0030().forwards(registry, None)

    row.refresh_from_db()
    assert row.sms_body == _migration_0030().NEW == defaults.ORIGINALS[row.key].sms_body


def test_0030_leaves_an_edited_confirmation_alone():
    from django.apps import apps as registry

    row = MessageTemplate.objects.get(key="trip_confirm_customer")
    row.sms_body = "Our own words for {trip_pickup_when}"
    row.save(update_fields=["sms_body"])

    _migration_0030().forwards(registry, None)

    row.refresh_from_db()
    assert row.sms_body == "Our own words for {trip_pickup_when}"
