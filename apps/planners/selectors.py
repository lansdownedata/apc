"""What a planner's pages read (APC-66/67). Plain dicts only: no model instance reaches a
planner template, so a price, a cost, an affiliate or an internal note cannot leak through
an attribute someone reaches for later — the fields simply aren't there."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Prefetch
from django.utils import dateformat, timezone

from apps.core.phone import to_e164
from apps.core.templatetags.phone_filters import phone_display
from apps.dispatch.models import Assignment
from apps.dispatch.selectors import coverage_prefetch
from apps.leads.models import Lead, LeadContact
from apps.public.wedding import format_clock, format_long_date
from apps.reservations.models import Reservation, Stop, today_at
from apps.tasks.models import Task

from . import services
from .models import PlannerAccount

# The number the public site prints (templates/public/_call_cta.html) — dispatch answers it
# around the clock. COMPANY_PHONE overrides it when set.
DEFAULT_DISPATCH_PHONE = "+12024242600"


def dispatch_phone() -> dict:
    number = to_e164(settings.COMPANY_PHONE) or DEFAULT_DISPATCH_PHONE
    return {"tel": number, "display": phone_display(number)}


def _title(lead: Lead) -> str:
    return lead.wedding_name.strip() or lead.contact.name


def _live(trips) -> list[Reservation]:
    return [t for t in trips if not t.is_cancelled]


def _first(trips) -> Reservation | None:
    dated = [t for t in trips if t.pickup_date is not None]
    if not dated:
        return None
    return min(dated, key=lambda t: (t.pickup_date, t.pickup_time or time.min, t.pk))


def _wedding_date(lead: Lead, trips) -> date | None:
    """The day the couple gave us on the intake, else the first trip's (trip-local) date."""
    iso = (lead.intake_payload or {}).get("wedding_date") or ""
    try:
        return date.fromisoformat(iso)
    except (TypeError, ValueError):
        first = _first(trips)
        return first.pickup_date if first else None


def _home_zone(trips) -> str:
    first = _first(trips)
    return (first.pickup_timezone if first else "") or ""


def _summary(lead: Lead, *, now) -> dict:
    trips = _live(lead.reservations.all())
    day = _wedding_date(lead, trips)
    return {
        "pk": lead.pk,
        "title": _title(lead),
        "reference": lead.quote_no,
        "date": day,
        "date_label": format_long_date(day) if day else "Date to be confirmed",
        "roles": sorted({r.get_role_display() for r in lead.planner_roles}),
        "archived": services.is_archived(lead.reservations.all(), now=now),
        "days_to_go": (day - today_at(_home_zone(trips))).days if day else None,
    }


def wedding_list(account: PlannerAccount) -> tuple[list[dict], list[dict]]:
    """(current, past). Current soonest first; past most recent first. Three queries."""
    now = timezone.now()
    leads = (
        services.weddings_for(account)
        .select_related("contact")
        .prefetch_related(
            "reservations",
            Prefetch(
                "people",
                queryset=LeadContact.objects.filter(contact_id=account.contact_id),
                to_attr="planner_roles",
            ),
        )
    )
    rows = [_summary(lead, now=now) for lead in leads]
    far = date.max
    current = sorted((r for r in rows if not r["archived"]), key=lambda r: r["date"] or far)
    past = sorted(
        (r for r in rows if r["archived"]), key=lambda r: r["date"] or date.min, reverse=True
    )
    return current, past


# The trip statuses at or past Dispatched (APC-67). Driver details appear only from here:
# dispatched means the driver has been told, so the planner can never learn a driver's name
# before the driver knows the job. Releasing driver info to the customer (APC-21) does not
# count. Both of LA's dispatched states are here, and the board's own Dispatched button
# writes DISPATCHED.
DISPATCHED_STATUSES = frozenset(
    {
        Reservation.TripStatus.DISPATCHED,
        Reservation.TripStatus.DISPATCHED_NON_LA,
        Reservation.TripStatus.ON_THE_WAY,
        Reservation.TripStatus.CIRCLING,
        Reservation.TripStatus.ARRIVED,
        Reservation.TripStatus.CUSTOMER_IN_CAR,
        Reservation.TripStatus.DONE,
    }
)

# The task kinds that wait on the planner, in the planner's words (label + due date only).
PLANNER_ITEMS = {
    "wedding_names": "Wedding names",
    "day_of_contact": "Day-of contact",
    "final_itinerary": "Final itinerary",
}

# A schedule exists once the order is confirmed; a quote's trips are still a proposal.
_SCHEDULED_STATUSES = (Lead.Status.BOOKED, Lead.Status.ENGAGED)


def _phone(number: str) -> dict | None:
    tel = to_e164(number) or ""
    return {"tel": tel, "display": phone_display(tel)} if tel else None


def _zone(trip: Reservation) -> ZoneInfo:
    return ZoneInfo(trip.pickup_timezone or settings.TIME_ZONE)


def _abbr(trip: Reservation) -> str:
    """The pickup zone's abbreviation on the trip's own day — always shown, never the
    viewer's zone (DST makes the day matter: EDT in October, EST in November)."""
    clock = trip.pickup_time or time(12, 0)
    return datetime.combine(trip.pickup_date, clock, tzinfo=_zone(trip)).tzname() or ""


def _place(stop: Stop) -> tuple[str, str]:
    """(what to call a stop, the line under it)."""
    name, address = stop.name.strip(), stop.address.strip()
    return (name, address) if name else (address or "Location to be confirmed", "")


def _driver(trip: Reservation) -> dict | None:
    """Name, cell and vehicle for a dispatched trip — never the affiliate's name."""
    if trip.trip_status not in DISPATCHED_STATUSES:
        return None
    active = next((a for a in trip.active_list if a.status == Assignment.Status.CONFIRMED), None)
    info = active.driver_info if active else None
    if not info:
        return None
    vehicle = " · ".join(
        p
        for p in (info["vehicle_desc"], info["vehicle_number"] and f"#{info['vehicle_number']}")
        if p
    )
    return {"name": info["name"], "cell": _phone(info["cell"] or ""), "vehicle": vehicle}


def _trip_row(trip: Reservation, home_abbr_for) -> dict:
    stops = list(trip.stops.all())
    origin, origin_sub = _place(stops[0]) if stops else ("Pickup to be confirmed", "")
    dest = _place(stops[-1])[0] if len(stops) > 1 else ""
    abbr = _abbr(trip)
    vehicle = trip.vehicle
    return {
        "pk": trip.pk,
        "time": format_clock(trip.pickup_time) if trip.pickup_time else "Time to be confirmed",
        "abbr": abbr,
        "away": abbr != home_abbr_for(trip.pickup_date),
        "from": origin,
        "from_sub": origin_sub,
        "to": dest,
        "extra_stops": max(len(stops) - 2, 0),
        "vehicle": vehicle.name if vehicle else "Vehicle to be confirmed",
        "seats": vehicle.capacity if vehicle else 0,
        "dispatched": trip.trip_status in DISPATCHED_STATUSES,
        "driver": _driver(trip),
    }


def _schedule(trips: list[Reservation], home: ZoneInfo) -> list[dict]:
    """Days in order; each day's runs (a linked set, or a lone trip) by their first pickup."""

    def home_abbr_for(day):
        return datetime.combine(day, time(12, 0), tzinfo=home).tzname()

    days: dict = {}
    for trip in trips:
        if trip.pickup_date is None:
            continue
        runs = days.setdefault(trip.pickup_date, {})
        key = str(trip.group_key) if trip.group_key else f"trip-{trip.pk}"
        run = runs.setdefault(key, {"key": key, "name": trip.service_label, "trips": []})
        run["trips"].append(_trip_row(trip, home_abbr_for))
    out = []
    for day in sorted(days):
        runs = list(days[day].values())
        for run in runs:
            run["vehicles"] = len(run["trips"])
            run["seats"] = sum(t["seats"] for t in run["trips"])
        out.append({"date": day, "label": f"{day:%A, %B} {day.day}", "runs": runs})
    return out


def _places(lead: Lead, trips: list[Reservation]) -> list[dict]:
    """Venues and hotels: every place the trips stop, hotels first when the intake named
    them. Before there are trips, what the couple told us on the intake."""
    payload = lead.intake_payload or {}
    hotels = {(h.get("name") or "").strip().lower() for h in payload.get("hotels") or []}
    if not trips:
        venue = (payload.get("venue_name") or "").strip()
        ceremony = ((payload.get("ceremony") or {}).get("name") or "").strip()
        names = [(h.get("name") or "").strip() for h in payload.get("hotels") or []]
        groups = [
            ("Ceremony", [{"name": ceremony, "sub": ""}] if ceremony else []),
            ("Venues", [{"name": venue, "sub": ""}] if venue else []),
            ("Hotels", [{"name": n, "sub": ""} for n in names if n]),
        ]
        return [{"label": label, "places": rows} for label, rows in groups if rows]
    seen: dict[str, dict] = {}
    for trip in trips:
        for stop in trip.stops.all():
            name, sub = _place(stop)
            seen.setdefault(name.lower(), {"name": name, "sub": sub})
    hotel_rows = [p for k, p in seen.items() if k in hotels]
    other = [p for k, p in seen.items() if k not in hotels]
    groups = [("Hotels", hotel_rows), ("Venues and other stops", other)]
    return [{"label": label, "places": rows} for label, rows in groups if rows]


def _outstanding(lead: Lead, home: ZoneInfo) -> list[dict]:
    rows = Task.objects.filter(lead=lead, status=Task.Status.OPEN, kind__in=PLANNER_ITEMS).order_by(
        "due_at", "pk"
    )
    now = timezone.now()
    return [
        {
            "label": PLANNER_ITEMS[t.kind],
            "due": f"Due {dateformat.format(t.due_at.astimezone(home), 'D, M j')}"
            if t.due_at
            else "",
            "overdue": bool(t.due_at and t.due_at < now),
        }
        for t in rows
    ]


def _coordinator(lead: Lead) -> dict | None:
    agent = lead.assigned_agent
    if agent is None:
        return None
    return {
        "name": agent.get_full_name() or agent.username,
        "phone": _phone(agent.phone),
        "email": agent.email,
    }


def dashboard(lead: Lead) -> dict:
    """Everything the wedding page shows, for one wedding the planner holds a role on.

    A fixed handful of queries whatever the size of the wedding: stops and the active
    assignment come from prefetches (never `Reservation.pickup`, whose `.order_by()`
    ignores them).
    """
    trips_qs = (
        Reservation.objects.select_related("vehicle", "service_type")
        .prefetch_related("stops", coverage_prefetch())
        .order_by("pickup_date", "pickup_time", "sort_order", "pk")
    )
    lead = (
        Lead.objects.select_related("contact", "assigned_agent")
        .prefetch_related(Prefetch("reservations", queryset=trips_qs))
        .get(pk=lead.pk)
    )
    lead.planner_roles = []
    summary = _summary(lead, now=timezone.now())
    trips = _live(lead.reservations.all())
    scheduled = lead.status in _SCHEDULED_STATUSES and bool(trips)
    shown = trips if scheduled else []
    home = ZoneInfo(_home_zone(trips) or settings.TIME_ZONE)
    schedule = _schedule(shown, home)
    guests = (lead.intake_payload or {}).get("guest_count")
    outstanding = _outstanding(lead, home)
    rows = [t for d in schedule for r in d["runs"] for t in r["trips"]]
    return {
        **summary,
        "guests": guests if isinstance(guests, int) and guests > 0 else None,
        "coordinator": _coordinator(lead),
        "places": _places(lead, shown),
        "scheduled": scheduled,
        "days": schedule,
        "trip_count": len(rows),
        "run_count": sum(len(d["runs"]) for d in schedule),
        "any_dispatched": any(
            t["dispatched"] for d in schedule for r in d["runs"] for t in r["trips"]
        ),
        "outstanding": outstanding,
    }


def access_for(contact) -> dict:
    """The contact profile's Planner access card (staff side): one line of state and which
    of Invite / Resend / Revoke apply. Two queries."""
    account = PlannerAccount.objects.filter(contact=contact).first()
    status = account.status if account else ""
    weddings = (
        Lead.objects.filter(people__contact=contact)
        .exclude(status=Lead.Status.LOST)
        .distinct()
        .count()
    )
    zone = timezone.get_current_timezone()

    def when(moment) -> str:
        return dateformat.format(moment.astimezone(zone), "M j, Y") if moment else ""

    if account is None:
        line = "Not invited"
    elif status == PlannerAccount.Status.REVOKED:
        line = f"Access revoked {when(account.revoked_at)}"
    elif status == PlannerAccount.Status.REGISTERED:
        line = f"Registered {when(account.registered_at)}"
        if account.last_seen_at:
            line += f" · last visit {when(account.last_seen_at)}"
    else:
        line = f"Invited {when(account.invited_at)} · not registered yet"
    return {
        "account": account,
        "line": line,
        "status": status,
        "has_email": bool(contact.email),
        "can_invite": bool(contact.email) and status in ("", PlannerAccount.Status.REVOKED),
        "can_resend": status == PlannerAccount.Status.INVITED,
        "can_revoke": status in (PlannerAccount.Status.INVITED, PlannerAccount.Status.REGISTERED),
        "weddings": weddings,
    }
