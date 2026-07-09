"""la_smoke_test walks the documented flow and stops at the first failure."""

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

from apps.integrations import limoanywhere


def test_not_configured_aborts_cleanly(settings):
    settings.LA_CLIENT_ID = ""
    out = StringIO()
    with pytest.raises(SystemExit):
        call_command("la_smoke_test", "--email", "t@example.com", stdout=out)
    assert "not configured" in out.getvalue().lower()


def test_happy_path_reports_each_step(settings, db):
    settings.LA_CLIENT_ID = "cid"
    settings.LA_CLIENT_SECRET = "cs"
    settings.LA_COMPANY_ALIAS = "allpro"
    out = StringIO()
    with (
        patch.object(limoanywhere, "get_token", return_value="tok"),
        patch.object(limoanywhere, "list_payment_types", return_value={"items": []}),
        patch.object(limoanywhere, "list_service_types", return_value={"items": []}),
        patch.object(limoanywhere, "list_vehicle_types", return_value={"items": []}),
        patch.object(limoanywhere, "validate_email", return_value={"available": True}),
        patch.object(limoanywhere, "register_customer", return_value={"id": 1, "number": "99"}),
        patch.object(limoanywhere, "rate_lookup", return_value={"results": [{"id": 5}]}),
        patch.object(
            limoanywhere, "create_booking", return_value={"id": 2, "confirmation_number": "C1"}
        ),
        patch.object(limoanywhere, "cancel_reservation", return_value={}),
        patch.object(limoanywhere, "subscribe_webhook", return_value=None),
    ):
        call_command("la_smoke_test", "--email", "t@example.com", stdout=out)
    text = out.getvalue()
    for step in ("token", "payment types", "register", "rate lookup", "booking", "cancel"):
        assert step in text.lower()
