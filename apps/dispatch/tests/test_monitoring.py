"""APC-23 — dispatch exception monitoring."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core import mail
from django.utils import timezone

from apps.dispatch import monitoring, services
from apps.dispatch.models import DispatchAlertConfig, DispatchException
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.notifications.models import Notification
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db

K = DispatchException.Kind
T = DispatchException.Tier
TS = Reservation.TripStatus


def _cfg(**over):
    cfg = DispatchAlertConfig.load()
    for k, v in over.items():
        setattr(cfg, k, v)
    cfg.save()
    return cfg


def _trip(delta, *, status="", covered=False, with_driver_info=True, with_affiliate_ack=True, **kw):
    when = timezone.localtime() + delta
    lead = kw.pop("lead", None) or LeadFactory(status=Lead.Status.BOOKED)
    trip = ReservationFactory(
        lead=lead,
        pickup_date=when.date(),
        pickup_time=when.time().replace(microsecond=0),
        trip_status=status,
        **kw,
    )
    if covered:
        a = services.assign_direct(trip, VendorFactory(), payout=1)
        # Driver info + affiliate ack present by default: these fixtures exist to test
        # the *other* milestones, and every one of them predates those two checks — give
        # them a value here so neither quietly joins every assertion. `with_driver_info=
        # False` / `with_affiliate_ack=False` opt back into the gap for the tests that
        # are actually about it.
        fields = []
        if with_driver_info:
            a.driver_name = "Sam Rivera"
            a.driver_cell = "+15715551212"
            fields += ["driver_name", "driver_cell"]
        if with_affiliate_ack:
            a.affiliate_confirmed_at = timezone.now()
            fields.append("affiliate_confirmed_at")
        if fields:
            a.save(update_fields=[*fields, "updated_at"])
    return trip


# --- config ---------------------------------------------------------------------------


def test_config_is_a_singleton():
    a = DispatchAlertConfig.load()
    b = DispatchAlertConfig.load()

    assert a.pk == b.pk == 1
    assert DispatchAlertConfig.objects.count() == 1


def test_email_list_parses_commas_and_newlines_and_falls_back(settings):
    settings.COMPANY_EMAIL = "ops@allprocharter.com"
    cfg = DispatchAlertConfig.load()
    assert cfg.email_list == ["ops@allprocharter.com"]

    cfg.alert_emails = "a@x.com, b@x.com\nc@x.com"
    assert cfg.email_list == ["a@x.com", "b@x.com", "c@x.com"]


def test_sms_list_is_empty_by_default():
    assert DispatchAlertConfig.load().sms_list == []


# --- evaluate ------------------------------------------------------------------------


def test_uncovered_trip_far_out_raises_nothing():
    cfg = _cfg()
    trip = _trip(timedelta(hours=30))

    assert monitoring.evaluate(trip, cfg, timezone.now()) == {}


def test_uncovered_trip_inside_the_warning_window():
    cfg = _cfg()
    trip = _trip(timedelta(hours=20))

    assert monitoring.evaluate(trip, cfg, timezone.now()) == {K.UNASSIGNED: T.WARNING}


def test_uncovered_trip_inside_the_critical_window():
    cfg = _cfg()
    trip = _trip(timedelta(hours=3))

    assert monitoring.evaluate(trip, cfg, timezone.now()) == {K.UNASSIGNED: T.CRITICAL}


def test_uncovered_trip_after_pickup_is_critical():
    cfg = _cfg()
    trip = _trip(timedelta(minutes=-30))

    assert monitoring.evaluate(trip, cfg, timezone.now())[K.UNASSIGNED] == T.CRITICAL


def test_covered_trip_not_en_route_near_pickup():
    cfg = _cfg()
    trip = _trip(timedelta(minutes=30), covered=True)

    assert monitoring.evaluate(trip, cfg, timezone.now()) == {K.NOT_ON_THE_WAY: T.WARNING}


def test_covered_trip_not_en_route_at_pickup_is_critical():
    cfg = _cfg()
    trip = _trip(timedelta(minutes=10), covered=True)

    assert monitoring.evaluate(trip, cfg, timezone.now())[K.NOT_ON_THE_WAY] == T.CRITICAL


def test_a_trip_marked_on_the_way_raises_no_en_route_exception():
    cfg = _cfg()
    trip = _trip(timedelta(minutes=10), covered=True, status=TS.ON_THE_WAY)

    assert K.NOT_ON_THE_WAY not in monitoring.evaluate(trip, cfg, timezone.now())


def test_covered_trip_not_arrived_after_pickup():
    cfg = _cfg()
    trip = _trip(timedelta(minutes=-20), covered=True, status=TS.ON_THE_WAY)

    assert monitoring.evaluate(trip, cfg, timezone.now())[K.NOT_ARRIVED] == T.WARNING


def test_covered_trip_still_not_arrived_well_past_pickup_is_critical():
    cfg = _cfg()
    trip = _trip(timedelta(minutes=-50), covered=True, status=TS.ON_THE_WAY)

    assert monitoring.evaluate(trip, cfg, timezone.now())[K.NOT_ARRIVED] == T.CRITICAL


def test_an_arrived_trip_raises_no_arrival_exception():
    cfg = _cfg()
    trip = _trip(timedelta(minutes=-50), covered=True, status=TS.ARRIVED)

    assert K.NOT_ARRIVED not in monitoring.evaluate(trip, cfg, timezone.now())


# --- driver info missing (APC-21) ------------------------------------------------------


def test_covered_farmed_out_trip_with_no_driver_info_warns():
    cfg = _cfg()
    trip = _trip(timedelta(hours=12), covered=True, with_driver_info=False)

    assert monitoring.evaluate(trip, cfg, timezone.now())[K.NO_DRIVER_INFO] == T.WARNING


def test_covered_farmed_out_trip_with_no_driver_info_close_to_pickup_is_critical():
    cfg = _cfg()
    trip = _trip(timedelta(hours=3), covered=True, with_driver_info=False)

    assert monitoring.evaluate(trip, cfg, timezone.now())[K.NO_DRIVER_INFO] == T.CRITICAL


def test_driver_info_on_file_raises_no_exception():
    cfg = _cfg()
    trip = _trip(timedelta(hours=3), covered=True)  # with_driver_info=True by default

    assert K.NO_DRIVER_INFO not in monitoring.evaluate(trip, cfg, timezone.now())


def test_an_in_house_assignment_always_has_driver_info():
    """No free-entry fields to be missing — Driver + Vehicle already carry it."""
    from apps.fleet.factories import DriverFactory

    cfg = _cfg()
    trip = _trip(timedelta(hours=3))
    services.assign_in_house(trip, DriverFactory())

    assert K.NO_DRIVER_INFO not in monitoring.evaluate(trip, cfg, timezone.now())


def test_an_uncovered_trip_raises_no_driver_info_exception():
    """An uncovered trip's real problem is UNASSIGNED — not this one too."""
    cfg = _cfg()
    trip = _trip(timedelta(hours=3))

    assert K.NO_DRIVER_INFO not in monitoring.evaluate(trip, cfg, timezone.now())


def test_a_done_trip_with_no_driver_info_raises_no_exception():
    """The trip already happened — nagging for driver info to release is pointless, same
    as NOT_ON_THE_WAY / NOT_ARRIVED not firing once their milestone is moot."""
    cfg = _cfg()
    trip = _trip(timedelta(hours=-2), status=TS.DONE, covered=True, with_driver_info=False)

    assert K.NO_DRIVER_INFO not in monitoring.evaluate(trip, cfg, timezone.now())


def test_entering_driver_info_resolves_the_open_exception():
    _cfg()
    trip = _trip(timedelta(hours=3), covered=True, with_driver_info=False)
    monitoring.run_dispatch_monitor()
    exc = DispatchException.objects.get(kind=K.NO_DRIVER_INFO)
    assert exc.is_open

    a = trip.assignments.get()
    a.driver_name, a.driver_cell = "Sam Rivera", "+15715551212"
    a.save(update_fields=["driver_name", "driver_cell", "updated_at"])
    monitoring.run_dispatch_monitor()

    exc.refresh_from_db()
    assert not exc.is_open


def test_a_trip_with_no_pickup_time_is_skipped():
    cfg = _cfg()
    trip = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED), pickup_date=None)

    assert monitoring.evaluate(trip, cfg, timezone.now()) == {}


# --- affiliate unacknowledged (APC-20) --------------------------------------------


def test_unacknowledged_farmed_out_trip_warns():
    cfg = _cfg()
    trip = _trip(timedelta(hours=12), covered=True, with_affiliate_ack=False)

    assert monitoring.evaluate(trip, cfg, timezone.now())[K.AFFILIATE_UNACKED] == T.WARNING


def test_affiliate_unacked_is_warning_only_even_close_to_pickup():
    """The client asked for a nudge here, not an escalating critical alert."""
    cfg = _cfg()
    trip = _trip(timedelta(minutes=30), covered=True, with_affiliate_ack=False)

    assert monitoring.evaluate(trip, cfg, timezone.now())[K.AFFILIATE_UNACKED] == T.WARNING


def test_acknowledged_affiliate_raises_no_exception():
    cfg = _cfg()
    trip = _trip(timedelta(hours=12), covered=True)  # with_affiliate_ack=True by default

    assert K.AFFILIATE_UNACKED not in monitoring.evaluate(trip, cfg, timezone.now())


def test_an_in_house_assignment_has_nothing_to_acknowledge():
    from apps.fleet.factories import DriverFactory

    cfg = _cfg()
    trip = _trip(timedelta(hours=12))
    services.assign_in_house(trip, DriverFactory())

    assert K.AFFILIATE_UNACKED not in monitoring.evaluate(trip, cfg, timezone.now())


def test_a_gnet_assignment_never_raises_unacked():
    """GNet has no email/ack-link flow, so affiliate_confirmed_at can never be set for
    one — without this exclusion every GNet trip would be flagged forever."""
    from apps.dispatch.models import Assignment

    cfg = _cfg()
    trip = _trip(timedelta(hours=12))
    a = services.assign_direct(trip, VendorFactory(), payout=1)
    a.channel = Assignment.Channel.GNET
    a.save(update_fields=["channel"])

    assert K.AFFILIATE_UNACKED not in monitoring.evaluate(trip, cfg, timezone.now())


def test_an_uncovered_trip_raises_no_affiliate_unacked_exception():
    cfg = _cfg()
    trip = _trip(timedelta(hours=12))

    assert K.AFFILIATE_UNACKED not in monitoring.evaluate(trip, cfg, timezone.now())


def test_a_trip_already_en_route_raises_no_affiliate_unacked_exception():
    """Visibly underway — the formal ack no longer matters."""
    cfg = _cfg()
    trip = _trip(
        timedelta(minutes=10), covered=True, with_affiliate_ack=False, status=TS.ON_THE_WAY
    )

    assert K.AFFILIATE_UNACKED not in monitoring.evaluate(trip, cfg, timezone.now())


def test_acknowledging_resolves_the_open_exception():
    _cfg()
    trip = _trip(timedelta(hours=12), covered=True, with_affiliate_ack=False)
    monitoring.run_dispatch_monitor()
    exc = DispatchException.objects.get(kind=K.AFFILIATE_UNACKED)
    assert exc.is_open

    a = trip.assignments.get()
    a.affiliate_confirmed_at = timezone.now()
    a.save(update_fields=["affiliate_confirmed_at", "updated_at"])
    monitoring.run_dispatch_monitor()

    exc.refresh_from_db()
    assert not exc.is_open


# --- run_dispatch_monitor ----------------------------------------------------------


def test_monitor_raises_and_records_an_exception():
    _cfg()
    _trip(timedelta(hours=3))

    raised = monitoring.run_dispatch_monitor()

    assert raised == 1
    exc = DispatchException.objects.get()
    assert exc.kind == K.UNASSIGNED
    assert exc.tier == T.CRITICAL
    assert exc.is_open


def test_a_disabled_monitor_does_nothing():
    _cfg(enabled=False)
    _trip(timedelta(hours=3))

    assert monitoring.run_dispatch_monitor() == 0
    assert not DispatchException.objects.exists()


def test_a_second_pass_with_no_change_raises_nothing():
    _cfg()
    _trip(timedelta(hours=3))
    monitoring.run_dispatch_monitor()

    assert monitoring.run_dispatch_monitor() == 0


def test_escalation_from_warning_to_critical_re_alerts():
    _cfg(unassigned_warn_hours=24, unassigned_critical_hours=4)
    trip = _trip(timedelta(hours=20))
    monitoring.run_dispatch_monitor()
    assert DispatchException.objects.get().tier == T.WARNING

    # move the trip inside the critical window
    when = timezone.localtime() + timedelta(hours=2)
    trip.pickup_date, trip.pickup_time = when.date(), when.time().replace(microsecond=0)
    trip.save(update_fields=["pickup_date", "pickup_time"])

    assert monitoring.run_dispatch_monitor() == 1
    assert DispatchException.objects.get().tier == T.CRITICAL


def test_a_met_milestone_resolves_its_exception():
    _cfg()
    trip = _trip(timedelta(hours=3))
    monitoring.run_dispatch_monitor()

    services.assign_direct(trip, VendorFactory(), payout=1)
    monitoring.run_dispatch_monitor()

    # A freshly-covered farmed-out trip immediately opens its own NO_DRIVER_INFO
    # exception (no roster to draw from) — this test is about the milestone that just
    # got met, UNASSIGNED, not that one.
    exc = DispatchException.objects.get(kind=K.UNASSIGNED)
    assert not exc.is_open
    assert exc.resolved_at is not None


def test_a_resolved_exception_reopens_if_it_breaches_again():
    _cfg()
    trip = _trip(timedelta(hours=3))
    monitoring.run_dispatch_monitor()
    a = services.assign_direct(trip, VendorFactory(), payout=1)
    monitoring.run_dispatch_monitor()  # resolves
    services.withdraw(a, note="fell through")

    raised = monitoring.run_dispatch_monitor()

    assert raised == 1
    assert DispatchException.objects.get(kind=K.UNASSIGNED).is_open


# --- alerting ---------------------------------------------------------------------


def test_new_exceptions_email_the_configured_recipients():
    _cfg(alert_emails="ops@allprocharter.com\nnight@allprocharter.com")
    _trip(timedelta(hours=3))
    mail.outbox.clear()

    monitoring.run_dispatch_monitor()

    assert {m.to[0] for m in mail.outbox} == {"ops@allprocharter.com", "night@allprocharter.com"}
    assert "CRITICAL" in mail.outbox[0].subject


def test_new_exceptions_land_in_the_notification_tray():
    _cfg()
    trip = _trip(timedelta(hours=3))

    monitoring.run_dispatch_monitor()

    note = Notification.objects.get(kind=Notification.Kind.DISPATCH_EXCEPTION)
    assert note.lead_id == trip.lead_id


def test_critical_tier_texts_the_sms_recipients():
    _cfg(critical_sms="+15715550100, +15715550101")
    _trip(timedelta(hours=3))

    with patch("apps.integrations.podium.send_message", return_value={"uid": "m"}) as send:
        monitoring.run_dispatch_monitor()

    assert {c.kwargs["identifier"] for c in send.call_args_list} == {
        "+15715550100",
        "+15715550101",
    }


def test_no_sms_recipients_means_no_texts():
    _cfg()
    _trip(timedelta(hours=3))

    with patch("apps.integrations.podium.send_message") as send:
        monitoring.run_dispatch_monitor()

    send.assert_not_called()


def test_a_warning_only_pass_sends_no_sms():
    _cfg(critical_sms="+15715550100")
    _trip(timedelta(hours=20))  # warning tier

    with patch("apps.integrations.podium.send_message") as send:
        monitoring.run_dispatch_monitor()

    send.assert_not_called()
