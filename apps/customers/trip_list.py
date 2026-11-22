"""Trips and history (spec §6.2, mockup 2) and the set the spreadsheet exports.

`filtered` is the one definition of "the trips this filter shows": the page counts, totals
and pages it, the export downloads all of it, so the two cannot drift apart. The database
picks and orders the trips (`selectors.with_labels`); a page then loads one page of them,
however long the history is.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Exists, F, OuterRef, Q, QuerySet, Subquery, Value
from django.db.models.functions import Lower, NullIf, Trim
from django.http import QueryDict
from django.utils import timezone

from apps.reservations.models import Passenger, Reservation, Stop

from . import selectors
from .models import CustomerAccount

PAGE_SIZE = 50
# The tabs and the group-by switch, in the mockup's order: (value, label).
TABS = (("upcoming", "Upcoming"), ("past", "Past"), ("requests", "Requests"), ("all", "All"))
GROUPINGS = (("month", "Month"), ("passenger", "Passenger"), ("reference", "Reference"))
# "Show more" asks for one more page each time; this keeps a hand-typed `page` sane.
MAX_PAGES = 200
QUERY_MAX = 100
# The rows the foot total sums (spec §7): completed trips and what was kept on cancellations.
# Both sit in the Past bucket, so the other tabs have nothing to total.
TOTALLED_KEYS = frozenset({"completed", "cancelled"})
_TOTALLED_TABS = frozenset({"past", "all"})
_NO_GROUP = {"passenger": "No passenger named", "reference": "No reference"}


@dataclass(frozen=True)
class TripFilter:
    """What the Trips page shows. `page` is which page of `PAGE_SIZE` rows: the page itself
    is always page 1, and "Show more" fetches the next one and appends it."""

    tab: str = "upcoming"
    start: date | None = None
    end: date | None = None
    q: str = ""
    passenger: str = ""
    group: str = "month"
    page: int = 1

    @classmethod
    def from_query(cls, query: QueryDict) -> TripFilter:
        """Read `?tab=&start=&end=&q=&passenger=&group=&page=`. A value it cannot use falls
        back to the default; a range typed backwards is read the right way round."""
        tab = query.get("tab", "")
        group = query.get("group", "")
        start, end = _iso_day(query.get("start", "")), _iso_day(query.get("end", ""))
        if start and end and start > end:
            start, end = end, start
        try:
            page = min(max(int(query.get("page", "1")), 1), MAX_PAGES)
        except ValueError:
            page = 1
        return cls(
            tab=tab if tab in dict(TABS) else cls.tab,
            start=start,
            end=end,
            q=query.get("q", "").strip()[:QUERY_MAX],
            passenger=query.get("passenger", "").strip()[:QUERY_MAX],
            group=group if group in dict(GROUPINGS) else cls.group,
            page=page,
        )

    @property
    def narrowed(self) -> bool:
        """Whether anything but the tab and grouping limits the trips."""
        return bool(self.start or self.end or self.q or self.passenger)

    def first_page(self) -> TripFilter:
        return replace(self, page=1)


def _iso_day(text: str) -> date | None:
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


def _matching(trips: QuerySet[Reservation], f: TripFilter) -> QuerySet[Reservation]:
    """`trips` narrowed by the date range (the trip's own local pickup date), the passenger
    and the search. Stops and passengers are matched with `Exists`, so a trip with two
    matching stops is still one row."""
    if f.start:
        trips = trips.filter(pickup_date__gte=f.start)
    if f.end:
        trips = trips.filter(pickup_date__lte=f.end)
    if f.passenger:
        rode = Passenger.objects.alias(clean=Trim("name")).filter(
            reservation=OuterRef("pk"), clean__iexact=f.passenger
        )
        trips = trips.filter(Exists(rode))
    if f.q:
        term = f.q
        stops = Stop.objects.filter(
            Q(address__icontains=term) | Q(name__icontains=term), reservation=OuterRef("pk")
        )
        people = Passenger.objects.filter(reservation=OuterRef("pk"), name__icontains=term)
        found = (
            Q(la_confirmation__icontains=term)
            | Q(customer_reference__icontains=term)
            | Q(Exists(stops))
            | Q(Exists(people))
        )
        pk = Reservation.pk_from_reference(term)
        if pk is not None:
            found |= Q(pk=pk)
        trips = trips.filter(found)
    return trips


def _lead_passenger_name() -> Subquery:
    """`selectors.lead_passenger` for the database: the lead passenger's name, else the
    first named one's."""
    named = (
        Passenger.objects.alias(clean=Trim("name"))
        .filter(reservation=OuterRef("pk"))
        .exclude(clean="")
        .order_by("-is_lead", "sort", "pk")
    )
    return Subquery(named.values("name")[:1])


def _in_order(trips: QuerySet[Reservation], tab: str, group: str) -> QuerySet[Reservation]:
    """Upcoming soonest first; every other tab newest first; undated trips last. By the
    trip's own local date and time, the ones the row shows, so a month never splits around
    trips in two zones. Grouped by passenger or reference: A to Z by that first, the blank
    group last."""
    day, clock = F("pickup_date"), F("pickup_time")
    if tab == "upcoming":
        by_time = (day.asc(nulls_last=True), clock.asc(nulls_first=True), "pk")
    else:
        by_time = (day.desc(nulls_last=True), clock.desc(nulls_last=True), "-pk")
    if group == "month":
        return trips.order_by(*by_time)
    title = _lead_passenger_name() if group == "passenger" else F("customer_reference")
    # Selected, not an alias: the database works a passenger's name out once per trip.
    return trips.annotate(group_title=NullIf(Lower(Trim(title)), Value(""))).order_by(
        F("group_title").asc(nulls_last=True), *by_time
    )


def filtered(account: CustomerAccount, f: TripFilter) -> QuerySet[Reservation]:
    """Every trip the filter shows, in the tab's order, unpaged and without stops or
    passengers (`selectors.load_details` adds them to the rows a caller shows). Carries
    `selectors.with_labels`' `label` and `bucket` to filter on."""
    found = _matching(selectors.with_labels(selectors.lean_trips(account)), f)
    if f.tab != "all":
        found = found.filter(bucket=f.tab)
    return _in_order(found, f.tab, f.group)


def export_trips(account: CustomerAccount, f: TripFilter) -> list[Reservation]:
    """Every trip the filter shows, whatever its page, with stops and passengers."""
    trips = list(filtered(account, f))
    selectors.load_details(trips)
    return trips


def ticked_trips(account: CustomerAccount, ids: list[int]) -> list[Reservation]:
    """The trips among `ids` the account can see (the rest are dropped), newest first, with
    stops and passengers."""
    return list(_in_order(selectors.visible_trips(account).filter(pk__in=ids), "all", "month"))


def rows_for(account: CustomerAccount, ids: list[int]) -> list[dict]:
    """The table rows of the trips among `ids` the account can see, as one group with no
    heading: what the page redraws after a reference is saved on them."""
    rows = selectors.trip_rows(ticked_trips(account, ids))
    return [{"title": "", "rows": rows, "continued": True}] if rows else []


def passenger_options(account: CustomerAccount) -> list[tuple[str, str]]:
    """Every named passenger on the account's trips, once each (case aside), A to Z: the
    passenger filter's options. One query; the page asks for it, a table fetch does not."""
    names = (
        Passenger.objects.filter(reservation__in=selectors.trips_in_scope(account).values("pk"))
        .values_list("name", flat=True)
        .order_by()
        .distinct()
    )
    unique: dict[str, str] = {}
    for name in names:
        name = name.strip()
        if name:
            unique.setdefault(name.casefold(), name)
    return [(name, name) for _, name in sorted(unique.items())]


def _group_title(trip: Reservation, group: str) -> str:
    """The trip's group heading; blank for no passenger or no reference."""
    if group == "month":
        day = trip.pickup_date
        return f"{day:%B %Y}" if day else "Date to be confirmed"
    if group == "passenger":
        return selectors.lead_passenger(trip)
    return trip.customer_reference.strip()


def _grouped(
    trips: list[Reservation], rows: list[dict], group: str, before: Reservation | None
) -> list[dict]:
    """Consecutive rows under one heading each. `continued` marks a first group that
    carries on from `before`, the last trip of the page above: its heading is already on
    the page, so a fetched page never repeats it."""

    def title_of(trip: Reservation) -> str:
        return _group_title(trip, group) or _NO_GROUP[group]

    groups: list[dict] = []
    last = title_of(before).casefold() if before else None
    for trip, row in zip(trips, rows, strict=True):
        title = title_of(trip)
        if not groups or title.casefold() != last:
            carries_on = not groups and title.casefold() == last
            groups.append({"title": title, "rows": [], "continued": carries_on})
        last = title.casefold()
        groups[-1]["rows"].append(row)
    return groups


def _day_text(day: date, year: bool = True) -> str:
    return f"{day:%b} {day.day}, {day.year}" if year else f"{day:%b} {day.day}"


def _range_texts(f: TripFilter) -> tuple[str, str]:
    """(header label, date field text): ("from Oct 1 to Nov 20, 2026", "Oct 1 – Nov 20,
    2026"). The year shows once when both ends share it. Both blank with no range."""
    start, end = f.start, f.end
    if start and end:
        if start == end:
            return f"on {_day_text(start)}", _day_text(start)
        first = _day_text(start, year=start.year != end.year)
        return f"from {first} to {_day_text(end)}", f"{first} – {_day_text(end)}"
    if start:
        return f"from {_day_text(start)}", f"From {_day_text(start)}"
    if end:
        return f"until {_day_text(end)}", f"Until {_day_text(end)}"
    return "", ""


def _page(
    found: QuerySet[Reservation], f: TripFilter, kept: dict[int, Decimal] | None = None
) -> dict:
    """Page `f.page` of `found`: the page's rows and the one before them (does the first
    group carry on?), their stops and passengers, and the kept amounts of any cancelled
    trip among them that `kept` does not already hold."""
    start = PAGE_SIZE * (f.page - 1)
    lead_in = 1 if start else 0
    window = list(found[start - lead_in : start + PAGE_SIZE + 1])
    before = window[0] if lead_in and window else None
    shown = window[lead_in : lead_in + PAGE_SIZE]
    selectors.load_details([before, *shown] if before and f.group == "passenger" else shown)
    kept = dict(kept or {})
    kept.update(selectors.kept_amounts(t for t in shown if t.pk not in kept))
    return {
        "groups": _grouped(shown, selectors.trip_rows(shown, kept=kept), f.group, before),
        "has_more": len(window) > lead_in + PAGE_SIZE,
    }


def page_of(account: CustomerAccount, f: TripFilter) -> dict:
    """One page of the filtered set, `f.page`: `groups` (`[{"title", "rows",
    "continued"}]`, in tab order) and `has_more`. This is all "Show more" fetches, so its
    cost and its size are one page's, whatever the page number."""
    return _page(filtered(account, f), f)


def trips(account: CustomerAccount, f: TripFilter) -> dict:
    """The Trips page (spec §6.2): page `f.page` (`page_of`), plus what describes the whole
    filtered set. `count`: its trips. `total`: the sum of each row's total over its
    Completed and Cancelled rows, the whole set and not only the rows shown, with `counted`
    the number of those rows. `has_trips`: whether the account has any trip at all.

    The count is a count; the total reads the set's completed and cancelled trips without
    stops or passengers (Past and All only: no other tab holds one) and their kept amounts
    once (`selectors.kept_amounts`: four queries when any is cancelled), never per row."""
    found = filtered(account, f)
    count = found.count()
    total, counted, kept = Decimal("0.00"), 0, {}
    if count and f.tab in _TOTALLED_TABS:
        # A total reads the trip, its order's stage and its review: no plan, no vehicle.
        priced = found.filter(label__in=TOTALLED_KEYS).order_by()
        totalled = list(priced.select_related(None).select_related("lead", "review"))
        kept = selectors.kept_amounts(totalled)
        amounts = [selectors.total_for(t, kept) for t in totalled]
        total = sum((amount for amount in amounts if amount is not None), total)
        counted = len(totalled)
    label, text = _range_texts(f)
    return {
        **(_page(found, f, kept) if count else {"groups": [], "has_more": False}),
        "count": count,
        "total": total,
        "counted": counted,
        "range_label": label,
        "range_text": text,
        "has_trips": bool(count) or selectors.trips_in_scope(account).exists(),
        "narrowed": f.narrowed,
    }


def quick_ranges() -> list[dict]:
    """The quick date ranges, from today in the business zone: this month, last month,
    this quarter, this year and last year, as ISO dates for the filter."""
    today = timezone.localdate()
    month = today.replace(day=1)
    last = (month - timedelta(days=1)).replace(day=1)
    quarter = month.replace(month=(today.month - 1) // 3 * 3 + 1)

    def span(label: str, start: date, end: date) -> dict:
        return {"label": label, "start": start.isoformat(), "end": end.isoformat()}

    def month_end(first: date) -> date:
        return (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)

    return [
        span("This month", month, month_end(month)),
        span("Last month", last, month_end(last)),
        span("This quarter", quarter, month_end(quarter.replace(month=quarter.month + 2))),
        span(str(today.year), date(today.year, 1, 1), date(today.year, 12, 31)),
        span(str(today.year - 1), date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)),
    ]
