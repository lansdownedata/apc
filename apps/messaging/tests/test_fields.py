import pytest

from apps.leads.factories import LeadFactory
from apps.messaging import fields, overrides
from apps.messaging.touchpoint_templates import TEMPLATES, build_context

KINDS = [*TEMPLATES, "thank_you_wedding"]


def _defaults(kind):
    t = overrides.default(kind)
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
    keys = lambda k: {f.key for f in fields.fields_for(k)}  # noqa: E731
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
    keys = {f.key for f in fields.fields_for(kind)}
    assert not keys & {"quote_link", "pay_link"}
    texts = {"subject": "S", "email_body": "E", "sms_body": "Hi {pay_link}"}
    assert "Unknown field {pay_link}" in fields.validate(kind, **texts)["sms_body"]


def test_trip_confirmations_keep_order_links():
    for kind in ("trip_confirm_customer", "trip_confirm_customer_2", "wed_final_details"):
        assert {"quote_link", "pay_link"} <= {f.key for f in fields.fields_for(kind)}


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
    keys = {f.key for f in fields.fields_for("review_request")}
    assert not keys & {"quote_link", "pay_link"}
    assert "review_link" in keys
