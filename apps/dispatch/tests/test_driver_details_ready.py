"""Driver details required — one setting, one check, every consumer."""

from datetime import time, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.dispatch import monitoring, services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment, DispatchAlertConfig
from apps.fleet.factories import DriverFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging.models import TouchPoint
from apps.reservations.factories import ReservationFactory
from apps.reservations.services import set_trip_status
from apps.tasks import services as tasks
from apps.tasks.models import Task

pytestmark = pytest.mark.django_db


def _trip():
    return ReservationFactory(
        lead=LeadFactory(status=Lead.Status.BOOKED),
        pickup_date=timezone.localdate() + timedelta(days=10),
        pickup_time=time(6, 15),
    )


def _confirmed(trip=None, **kw):
    return AssignmentFactory(reservation=trip or _trip(), status=Assignment.Status.CONFIRMED, **kw)


def _set(a, **over):
    fields = {"name": "Sam Rivera", "cell": "", "vehicle_desc": "", "vehicle_number": ""}
    fields.update(over)
    return services.set_driver_info(a, **fields)


def _cfg(**over):
    cfg = DispatchAlertConfig.load()
    for k, v in over.items():
        setattr(cfg, k, v)
    cfg.save()
    return cfg


def test_default_requires_name_and_cell():
    a = _confirmed()
    _set(a)
    cfg = DispatchAlertConfig.load()

    assert services.missing_driver_fields(a, cfg) == ["driver's cell"]
    assert services.driver_details_ready(a, cfg) is False

    _set(a, cell="+15715551212")

    assert services.missing_driver_fields(a, cfg) == []
    assert services.driver_details_ready(a, cfg) is True


def test_nothing_on_file_misses_every_required_label():
    a = _confirmed()
    cfg = _cfg(require_vehicle_desc=True, require_vehicle_number=True)

    assert services.missing_driver_fields(a, cfg) == [
        "driver's name",
        "driver's cell",
        "vehicle description",
        "vehicle number",
    ]


def test_vehicle_number_required_when_ticked():
    a = _confirmed()
    _set(a, cell="+15715551212")
    cfg = _cfg(require_vehicle_number=True)

    assert cfg.required_driver_fields() == ("name", "cell", "vehicle_number")
    assert services.missing_driver_fields(a, cfg) == ["vehicle number"]


def test_in_house_reads_driver_and_vehicle():
    a = services.assign_in_house(_trip(), DriverFactory(phone=""))

    assert services.missing_driver_fields(a, DispatchAlertConfig.load()) == ["driver's cell"]


def test_task_closes_only_when_ready():
    trip = ReservationFactory(
        lead=LeadFactory(status=Lead.Status.BOOKED),
        pickup_date=timezone.localdate() + timedelta(days=20),
        pickup_time=time(9, 0),
    )
    lead = trip.lead
    a = _confirmed(trip)
    _set(a)
    tasks.evaluate_lead(lead)
    task = Task.objects.get(lead=lead, kind="driver_info_received", reservation=trip)
    assert task.status != Task.Status.DONE

    _set(a, cell="+15715551212")

    task.refresh_from_db()
    assert task.status == Task.Status.DONE


def test_monitor_alerts_until_ready():
    trip = _trip()
    a = _confirmed(trip)
    _set(a)
    cfg = DispatchAlertConfig.load()
    now = trip.pickup_at - timedelta(hours=5)

    assert monitoring.evaluate(trip, cfg, now)[monitoring.K.NO_DRIVER_INFO] == monitoring.T.CRITICAL

    _set(a, cell="+15715551212")

    assert monitoring.K.NO_DRIVER_INFO not in monitoring.evaluate(trip, cfg, now)


def test_customer_message_waits_for_ready():
    trip = _trip()
    a = _confirmed(trip)
    drv = TouchPoint.Kind.DRIVER_RELEASED

    set_trip_status(trip, "dispatched")
    _set(a)
    assert not TouchPoint.objects.filter(reservation=trip, kind=drv).exists()

    _set(a, cell="+15715551212")
    assert TouchPoint.objects.filter(reservation=trip, kind=drv).count() == 1


def test_turnaround_stamp_uses_setting():
    _cfg(require_vehicle_desc=True)
    a = _confirmed()

    _set(a, cell="+15715551212")
    a.refresh_from_db()
    assert a.driver_info_at is None

    _set(a, cell="+15715551212", vehicle_desc="Black Suburban")
    a.refresh_from_db()
    assert a.driver_info_at is not None


def test_settings_form_saves_flags(client):
    client.force_login(UserFactory(role="owner_admin"))
    cfg = DispatchAlertConfig.load()
    data = {
        "enabled": "on",
        "require_driver_cell": "on",
        "require_vehicle_number": "on",
    }
    for name in (
        "unassigned_warn_hours",
        "unassigned_critical_hours",
        "otw_warn_minutes",
        "otw_critical_minutes",
        "arrived_warn_minutes",
        "arrived_critical_minutes",
        "driver_info_warn_hours",
        "driver_info_critical_hours",
        "affiliate_unacked_warn_hours",
    ):
        data[name] = getattr(cfg, name)

    resp = client.post(reverse("dispatch_alerts"), data)

    assert resp.status_code == 302
    cfg = DispatchAlertConfig.load()
    assert cfg.require_vehicle_number is True
    assert cfg.require_vehicle_desc is False
    assert cfg.require_driver_cell is True
    page = client.get(reverse("dispatch_alerts")).content.decode()
    assert "Required before driver details go to the customer" in page


def _drawer(client, a):
    client.force_login(UserFactory())
    return client.get(reverse("dispatch_assign_panel", args=[a.reservation_id])).content.decode()


def test_drawer_says_what_the_customer_message_waits_for(client):
    a = _confirmed()
    _set(a, name="Sam Rivera")

    page = _drawer(client, a)

    assert "Customer message waits for: driver&#x27;s cell" in page
    assert (
        "Once the required details are saved, the customer is sent them automatically." not in page
    )


def test_drawer_shows_no_hold_once_the_required_details_are_saved(client):
    a = _confirmed()
    _set(a, name="Sam Rivera", cell="555-0100")

    page = _drawer(client, a)

    assert "Customer message waits for" not in page
    assert "not on file" not in page


def test_drawer_hint_names_the_required_details_rule(client):
    a = _confirmed()

    page = _drawer(client, a)

    assert "Once the required details are saved, the customer is sent them automatically." in page
    assert "Saving a name here" not in page


def test_drawer_query_count_is_the_same_held_or_ready(client):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    held = _confirmed()
    _set(held, name="Sam Rivera")
    ready = _confirmed()
    _set(ready, name="Sam Rivera", cell="555-0100")
    client.force_login(UserFactory())
    client.get(reverse("dispatch_assign_panel", args=[ready.reservation_id]))  # warm caches

    with CaptureQueriesContext(connection) as ready_q:
        client.get(reverse("dispatch_assign_panel", args=[ready.reservation_id]))
    with CaptureQueriesContext(connection) as held_q:
        client.get(reverse("dispatch_assign_panel", args=[held.reservation_id]))
    assert len(held_q) == len(ready_q)
