import pytest

from apps.leads.factories import LeadFactory
from apps.messaging import fields
from apps.messaging.defaults import DEFAULTS
from apps.messaging.touchpoint_templates import TEMPLATES, build_context
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Passenger

KINDS = [*TEMPLATES, "thank_you_wedding"]


def _defaults(kind):
    t = DEFAULTS[kind]
    return {"subject": t.subject, "email_body": t.email_body, "sms_body": t.sms_body}


@pytest.mark.django_db
def test_registry_covers_build_context():
    assert set(fields.FIELDS) == set(build_context(LeadFactory()))


@pytest.mark.parametrize("kind", KINDS)
def test_every_default_validates(kind):
    assert fields.validate(kind, **_defaults(kind)) == {}


def test_unknown_field():
    texts = _defaults("tp8_expired") | {"sms_body": "Hi {frist_name} {quote_link}"}
    assert fields.validate("tp8_expired", **texts) == {
        "sms_body": "Unknown field {frist_name}. Use the buttons below to insert fields."
    }


def test_field_not_for_this_kind():
    texts = _defaults("tp1_welcome") | {"sms_body": "Call {driver_cell}"}
    assert "Unknown field {driver_cell}" in fields.validate("tp1_welcome", **texts)["sms_body"]


def test_bad_braces():
    texts = _defaults("tp1_welcome") | {"sms_body": "Hi {first_name"}
    assert fields.validate("tp1_welcome", **texts) == {
        "sms_body": "Unmatched { or }. To show a brace, type {{ or }}."
    }
    texts = _defaults("tp1_welcome") | {"sms_body": "Use {{ and }}"}
    assert fields.validate("tp1_welcome", **texts) == {}


def test_format_spec_is_refused_not_ignored():
    texts = _defaults("tp1_welcome") | {"sms_body": "{first_name!r} {first_name:>5}"}
    assert "Formatting isn't supported" in fields.validate("tp1_welcome", **texts)["sms_body"]


def test_required_link():
    texts = _defaults("payment_reminder")
    texts["email_body"] = texts["email_body"].replace("{pay_link}", "")
    texts["sms_body"] = texts["sms_body"].replace("{pay_link}", "")
    errors = fields.validate("payment_reminder", **texts)
    assert "{pay_link}" in errors["sms_body"]
    assert "pays" in errors["sms_body"]
    assert len(errors) == 1
    texts["email_body"] += " {pay_link}"
    assert fields.validate("payment_reminder", **texts) == {}


def test_empty_channel():
    texts = _defaults("order_confirmed") | {"sms_body": ""}
    assert fields.validate("order_confirmed", **texts) == {
        "sms_body": "Can't be empty — this message sends by SMS."
    }


def test_empty_subject_on_email_kind():
    texts = _defaults("tp2_lead_followup") | {"subject": ""}
    assert fields.validate("tp2_lead_followup", **texts) == {
        "subject": "Can't be empty — this message sends by email."
    }


def test_affiliate_has_no_customer_fields():
    texts = _defaults("trip_confirm_affiliate") | {"email_body": "{first_name} {ack_link}"}
    assert (
        "Unknown field {first_name}"
        in fields.validate("trip_confirm_affiliate", **texts)["email_body"]
    )


def test_fields_for_groups():
    keys = lambda k: {f.key for f in fields.fields_for_kind(k)}  # noqa: E731
    assert "pay_link" in keys("tp1_welcome") and "confirm_link" not in keys("tp1_welcome")
    assert "confirm_link" in keys("thank_you") and "review_link" not in keys("thank_you")
    assert "review_link" in keys("review_request")
    assert "driver_name" in keys("driver_updated") and "driver_name" not in keys(
        "trip_confirm_customer"
    )
    assert "trip_sheet" in keys("trip_confirm_customer")
    assert keys("trip_confirm_affiliate") >= {"ack_link", "payout", "driver_name", "company_name"}
    assert not keys("trip_confirm_affiliate") & {
        "first_name",
        "quote_link",
        "pay_link",
        "balance_amount",
    }


def _row(**over):
    """A valid added customer row on the pickup trigger; override to break it."""
    row = {
        "trigger": "pickup",
        "audience": "customer",
        "send_sms": True,
        "send_email": False,
        "subject": "",
        "email_body": "",
        "sms_body": "Hi {first_name}",
        "recipients": ["booking_contact"],
        "staff_emails": [],
        "timing_direction": "before",
    }
    return row | over


def _offered(trigger, audience="customer", key=""):
    return {f.key for f in fields.fields_for(trigger, audience, key)}


def test_valid_row_has_no_errors():
    assert fields.validate(_row()) == {}


@pytest.mark.parametrize("key", list(DEFAULTS))
def test_every_default_validates_as_a_row(key):
    d = DEFAULTS[key]
    row = {
        "key": key,
        "seeded": True,
        "trigger": d.trigger,
        "audience": d.audience,
        "send_sms": d.send_sms,
        "send_email": d.send_email,
        "subject": d.subject,
        "email_body": d.email_body,
        "sms_body": d.sms_body,
        "recipients": ["booking_contact"],
        "staff_emails": ["a@example.com"],
        "timing_direction": d.timing_direction,
    }
    assert fields.validate(row) == {}


def test_offers_follow_the_trigger_not_the_kind():
    assert {"first_name", "quote_link", "pay_link", "company_name"} <= _offered("quote_sent")
    assert not _offered("quote_sent") & {"trip_sheet", "driver_name", "vendor_name", "confirm_link"}
    assert "trip_sheet" in _offered("pickup") and "driver_name" not in _offered("pickup")
    assert "driver_name" in _offered("pickup_dispatched")
    assert _offered("deposit_report", "staff") == {"company_name", "company_phone", "company_email"}
    assert "trip_passenger_names" in _offered("pickup")
    assert "trip_passenger_names" not in _offered("quote_sent")


@pytest.mark.parametrize("trigger", ["pickup", "affiliate_accepted", "trip_cancelled"])
def test_affiliate_fields_only_for_the_affiliate_audience(trigger):
    assert {"vendor_name", "payout"} <= _offered(trigger, "affiliate")
    for audience in ("customer", "planner", "staff"):
        assert not _offered(trigger, audience) & {"vendor_name", "payout"}


def test_customer_row_cannot_use_payout():
    errors = fields.validate(_row(sms_body="Pay {payout}"))
    assert errors == {"sms_body": "Unknown field {payout}. Use the buttons below to insert fields."}
    assert fields.validate(_row(audience="affiliate", sms_body="Pay {payout}")) == {}


def test_confirm_link_only_on_trip_level_non_affiliate():
    assert "confirm_link" in _offered("pickup", "customer")
    assert "confirm_link" in _offered("status_arrived", "planner")
    assert "confirm_link" not in _offered("pickup", "affiliate")
    assert "confirm_link" not in _offered("quote_sent")
    assert "confirm_link" not in _offered("balance_paid", "planner")
    assert (
        "Unknown field {confirm_link}"
        in fields.validate(
            _row(trigger="quote_sent", timing_direction="after", sms_body="Go {confirm_link}")
        )["sms_body"]
    )


def test_ack_and_review_links_only_for_their_seeded_message():
    for trigger in ("pickup", "affiliate_accepted", "all_trips_done", "quote_sent"):
        for audience in ("customer", "affiliate", "planner"):
            assert not _offered(trigger, audience) & {"ack_link", "review_link"}
    assert "ack_link" in _offered("pickup", "affiliate", "trip_confirm_affiliate")
    assert "review_link" in _offered("all_trips_done", "customer", "review_request")
    # off its original trigger the seeded message loses them
    assert "ack_link" not in _offered("trip_cancelled", "affiliate", "trip_confirm_affiliate")
    errors = fields.validate(_row(audience="affiliate", sms_body="Ack {ack_link}"))
    assert errors["sms_body"].startswith("Unknown field {ack_link}")


def test_seeded_message_on_its_trigger_keeps_required_link():
    d = DEFAULTS["payment_reminder"]
    row = {
        "key": "payment_reminder",
        "seeded": True,
        "trigger": d.trigger,
        "audience": "customer",
        "send_sms": True,
        "send_email": True,
        "subject": d.subject,
        "email_body": d.email_body.replace("{pay_link}", ""),
        "sms_body": d.sms_body.replace("{pay_link}", ""),
        "recipients": ["booking_contact"],
        "staff_emails": [],
        "timing_direction": d.timing_direction,
    }
    errors = fields.validate(row)
    assert "{pay_link}" in errors["sms_body"] and "pays" in errors["sms_body"]
    assert len(errors) == 1
    # off its original trigger, or as an added row, nothing is required
    assert fields.validate(row | {"trigger": "quote_sent", "timing_direction": "after"}) == {}
    assert fields.validate(row | {"seeded": False}) == {}


def test_row_level_checks():
    assert fields.validate(_row(send_sms=False, send_email=False)) == {
        "channels": "Turn on Text or Email."
    }
    assert fields.validate(_row(recipients=[])) == {"recipients": "Tick at least one person."}
    assert fields.validate(
        _row(
            audience="staff",
            trigger="deposit_report",
            timing_direction="after",
            recipients=[],
            staff_emails=[],
            sms_body="x",
        )
    ) == {"staff_emails": "Add at least one email."}
    assert fields.validate(_row(trigger="quote_sent")) == {
        "timing_direction": "Before only works with a date."
    }
    assert fields.validate(_row(trigger="nope")) == {"trigger": "Pick when this message goes out."}


def test_email_needs_subject_and_body_and_text_needs_body():
    row = _row(send_sms=True, send_email=True, email_body="Hello {first_name}", subject=" ")
    assert fields.validate(row) == {"subject": "Add a subject for the email."}
    assert fields.validate(row | {"subject": "S", "email_body": ""}) == {
        "email_body": "Can't be empty — this message sends by email."
    }
    assert fields.validate(_row(sms_body="")) == {
        "sms_body": "Can't be empty — this message sends by SMS."
    }
    assert fields.validate(_row(send_sms=False, send_email=True, email_body="x", subject="s")) == {}


def test_row_box_errors_use_the_same_wording():
    assert fields.validate(_row(sms_body="Hi {first_name"))["sms_body"] == (
        "Unmatched { or }. To show a brace, type {{ or }}."
    )
    assert (
        "Formatting isn't supported" in fields.validate(_row(sms_body="{first_name!r}"))["sms_body"]
    )


@pytest.mark.django_db
def test_passenger_names_fill_from_names_only():
    res = ReservationFactory()
    Passenger.objects.create(reservation=res, name="Dana", mobile="+15551230000", sort=0)
    Passenger.objects.create(reservation=res, name="Lee", mobile="+15551239999", sort=1)
    ctx = build_context(res.lead, res)
    assert ctx["trip_passenger_names"] == "Dana, Lee"
    assert "555" not in ctx["trip_passenger_names"]
    assert build_context(res.lead)["trip_passenger_names"] == ""


def test_required_for():
    assert fields.required_for("payment_reminder") == {"pay_link"}
    assert fields.required_for("tp1_welcome") == frozenset()


@pytest.mark.parametrize(
    "kind",
    [
        "driver_released",
        "driver_updated",
        "status_on_the_way",
        "status_arrived",
        "status_dispatched",
    ],
)
def test_trip_day_kinds_get_no_order_links(kind):
    keys = {f.key for f in fields.fields_for_kind(kind)}
    assert not keys & {"quote_link", "pay_link"}
    texts = {"subject": "S", "email_body": "E", "sms_body": "Hi {pay_link}"}
    assert "Unknown field {pay_link}" in fields.validate(kind, **texts)["sms_body"]


def test_trip_confirmations_keep_order_links():
    for kind in ("trip_confirm_customer", "trip_confirm_customer_2", "wed_final_details"):
        assert {"quote_link", "pay_link"} <= {f.key for f in fields.fields_for_kind(kind)}


@pytest.mark.parametrize("text", ["{first_name: }", "{first_name:x}", "{first_name!r}"])
def test_format_spec_or_conversion_refused(text):
    texts = _defaults("tp1_welcome") | {"sms_body": f"Hi {text}"}
    assert fields.validate("tp1_welcome", **texts) == {
        "sms_body": (
            "Formatting isn't supported inside a field. Use the buttons below to insert fields."
        )
    }


def test_escaped_braces_stay_valid():
    texts = _defaults("tp1_welcome") | {"sms_body": "Hi {first_name} {{ok}}"}
    assert fields.validate("tp1_welcome", **texts) == {}


def test_review_request_gets_no_order_links():
    keys = {f.key for f in fields.fields_for_kind("review_request")}
    assert not keys & {"quote_link", "pay_link"}
    assert "review_link" in keys


def test_wording_errors_are_only_the_ones_that_stop_rendering():
    data = {
        "trigger": "lead_created",
        "audience": "customer",
        "send_sms": True,
        "send_email": False,
        "subject": "",
        "email_body": "{driver_cell}",
        "sms_body": "Hi {first_name",
        "recipients": [],
        "staff_emails": [],
        "timing_direction": "after",
    }
    assert set(fields.validate(data)) == {"recipients", "email_body", "sms_body"}
    assert set(fields.wording_errors(data)) == {"email_body", "sms_body"}
    fine = data | {"email_body": "", "sms_body": "Hi {first_name}"}
    assert fields.wording_errors(fine) == {}
