"""Everything the task predicates read, loaded once per batch of orders (APC-50).

`load_facts` answers for many leads in a fixed number of queries — one per source table —
so generation for a 10-trip order and the cron's pass over every open task cost the same
handful of queries however many trips or tasks are involved.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from apps.contacts.models import Contact
from apps.dispatch.models import Assignment
from apps.leads.models import CustomerFeedback, Lead, LeadContact
from apps.messaging.models import TouchPoint
from apps.payments.models import PaymentPlan
from apps.reservations.models import Reservation, TripReview
from apps.reservations.services import is_wedding_trip


@dataclass
class LeadFacts:
    lead: Lead
    plan: PaymentPlan | None = None
    trips: list[Reservation] = field(default_factory=list)
    assignments: dict[int, Assignment] = field(default_factory=dict)
    released_trip_ids: set[int] = field(default_factory=set)
    # Completed post-trip reviews by trip id (APC-59).
    reviews: dict[int, TripReview] = field(default_factory=dict)
    feedback: CustomerFeedback | None = None  # APC-63
    day_of_contact: Contact | None = None  # APC-64 — the day-of coordinator role
    # APC-58. Set by `services.ensure_tasks`, which knows the clock and the grace; empty
    # elsewhere, where only predicates run and neither is read.
    ended_trip_ids: set[int] = field(default_factory=set)
    post_trip_ids: set[int] = field(default_factory=set)

    @property
    def live_trips(self) -> list[Reservation]:
        return [t for t in self.trips if not t.is_cancelled]

    @property
    def is_wedding(self) -> bool:
        """The workspace's rule: wedding answers already on file, or any wedding trip."""
        return bool(self.lead.wedding_name) or any(is_wedding_trip(t) for t in self.trips)

    @property
    def last_pickup_at(self):
        times = [t.pickup_at for t in self.live_trips if t.pickup_at is not None]
        return max(times) if times else None

    @property
    def first_pickup_at(self):
        """The order's anchor for order-level due dates — its earliest live pickup."""
        times = [t.pickup_at for t in self.live_trips if t.pickup_at is not None]
        return min(times) if times else None

    def active_assignment(self, reservation_id: int | None) -> Assignment | None:
        return self.assignments.get(reservation_id)


def load_facts(leads: Iterable[Lead]) -> dict[int, LeadFacts]:
    """Facts for every lead in `leads`, keyed by lead pk. Seven queries, whatever the size.

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
    # `payable` rides along so the payable predicates (APC-61) cost no query per trip.
    for a in active.select_related("vendor", "driver", "vehicle", "payable"):
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

    done = TripReview.objects.filter(reservation__lead_id__in=ids, completed_at__isnull=False)
    for review in done:
        facts[by_trip[review.reservation_id]].reviews[review.reservation_id] = review

    for fb in CustomerFeedback.objects.filter(lead_id__in=ids):
        facts[fb.lead_id].feedback = fb

    day_of = LeadContact.objects.filter(
        lead_id__in=ids, role=LeadContact.Role.DAY_OF_COORDINATOR
    ).select_related("contact")
    for row in day_of.order_by("-created_at", "-pk"):  # oldest last, so it wins
        facts[row.lead_id].day_of_contact = row.contact
    return facts
