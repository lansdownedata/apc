"""Everything the task predicates read, loaded once per batch of orders (APC-50).

`load_facts` answers for many leads in a fixed number of queries — one per source table —
so generation for a 10-trip order and the cron's pass over every open task cost the same
handful of queries however many trips or tasks are involved.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from apps.dispatch.models import Assignment
from apps.leads.models import Lead
from apps.messaging.models import TouchPoint
from apps.payments.models import PaymentPlan
from apps.reservations.models import Reservation
from apps.reservations.services import is_wedding_trip


@dataclass
class LeadFacts:
    lead: Lead
    plan: PaymentPlan | None = None
    trips: list[Reservation] = field(default_factory=list)
    assignments: dict[int, Assignment] = field(default_factory=dict)
    released_trip_ids: set[int] = field(default_factory=set)

    @property
    def live_trips(self) -> list[Reservation]:
        return [t for t in self.trips if not t.is_cancelled]

    @property
    def is_wedding(self) -> bool:
        """The workspace's rule: wedding answers already on file, or any wedding trip."""
        return bool(self.lead.wedding_name) or any(is_wedding_trip(t) for t in self.trips)

    @property
    def first_pickup_at(self):
        """The order's anchor for order-level due dates — its earliest live pickup."""
        times = [t.pickup_at for t in self.live_trips if t.pickup_at is not None]
        return min(times) if times else None

    def active_assignment(self, reservation_id: int | None) -> Assignment | None:
        return self.assignments.get(reservation_id)


def load_facts(leads: Iterable[Lead]) -> dict[int, LeadFacts]:
    """Facts for every lead in `leads`, keyed by lead pk. Four queries, whatever the size.

    Predicates read `lead` fields straight off these instances, so pass fresh rows.
    """
    facts = {lead.pk: LeadFacts(lead=lead) for lead in leads}
    if not facts:
        return facts
    ids = list(facts)

    for plan in PaymentPlan.objects.filter(lead_id__in=ids):
        facts[plan.lead_id].plan = plan

    trips = Reservation.objects.filter(lead_id__in=ids).select_related("service_type")
    for trip in trips.order_by("pickup_date", "pickup_time", "pk"):
        facts[trip.lead_id].trips.append(trip)

    by_trip: dict[int, int] = {}
    for f in facts.values():
        for trip in f.trips:
            by_trip[trip.pk] = f.lead.pk

    active = Assignment.objects.active().filter(reservation__lead_id__in=ids)
    for a in active.select_related("vendor", "driver", "vehicle"):
        facts[by_trip[a.reservation_id]].assignments[a.reservation_id] = a

    released: dict[int, set[int]] = defaultdict(set)
    sent = TouchPoint.objects.filter(
        lead_id__in=ids,
        kind=TouchPoint.Kind.DRIVER_RELEASED,
        status=TouchPoint.Status.SENT,
        reservation__isnull=False,
    ).values_list("lead_id", "reservation_id")
    for lead_id, reservation_id in sent:
        released[lead_id].add(reservation_id)
    for lead_id, trip_ids in released.items():
        facts[lead_id].released_trip_ids = trip_ids
    return facts
