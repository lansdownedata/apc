"""The dispatch board's filter state (APC-24).

One object parsed from the querystring: the date window (a day, a week, or a custom
range) plus the vehicle-type / customer / linked-set / coverage filters. Kept separate
from the view so the parse and the nav-link maths are unit-testable, and separate from
`selectors` so the selector just takes a resolved `(start, end)` and a few ids.

Readable query params by design (`?view=week&day=…&vehicle=3&customer=42&f=uncovered`) —
a signed single-blob param was considered and deferred (bookmarkable, debuggable URLs win
for an internal board).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlencode

from django.http import HttpRequest
from django.utils import timezone

VIEWS = ("day", "week", "range")
COVERAGE_BUCKETS = ("uncovered", "offered", "confirmed")


def _date(raw: str | None) -> date | None:
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        return None


def _int(raw: str | None) -> int | None:
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _uuid(raw: str | None) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        return None


@dataclass(frozen=True)
class BoardFilters:
    """Resolved board state. `start`/`end` are inclusive trip-local dates."""

    # A runaway `pickup_date__range` is the one thing that can make this query slow, so the
    # custom range is capped. ~13 weeks covers any real multi-day programme.
    MAX_SPAN_DAYS = 92

    view: str
    start: date
    end: date
    anchor: date
    vehicle_type_id: int | None = None
    contact_id: int | None = None
    group_key: str | None = None
    coverage: str = ""

    @classmethod
    def from_request(cls, request: HttpRequest) -> BoardFilters:
        g = request.GET
        today = timezone.localdate()

        view = g.get("view", "day")
        if view not in VIEWS:
            view = "day"

        if view == "range":
            start = _date(g.get("start")) or today
            end = _date(g.get("end")) or start
            if end < start:
                start, end = end, start
            if (end - start).days > cls.MAX_SPAN_DAYS:
                end = start + timedelta(days=cls.MAX_SPAN_DAYS)
            anchor = start
        else:
            anchor = _date(g.get("day")) or today
            if view == "week":
                anchor -= timedelta(days=anchor.weekday())  # back to Monday
                start, end = anchor, anchor + timedelta(days=6)
            else:
                start = end = anchor

        coverage = g.get("f", "")
        return cls(
            view=view,
            start=start,
            end=end,
            anchor=anchor,
            vehicle_type_id=_int(g.get("vehicle")),
            contact_id=_int(g.get("customer")),
            group_key=_uuid(g.get("group")),
            coverage=coverage if coverage in COVERAGE_BUCKETS else "",
        )

    # --- rendering helpers ---
    @property
    def is_multi_day(self) -> bool:
        return self.start != self.end

    @property
    def span_days(self) -> int:
        return (self.end - self.start).days + 1

    def _filter_params(self) -> list[tuple[str, str]]:
        """The non-date filters, carried across every nav link and view switch."""
        out: list[tuple[str, str]] = []
        if self.vehicle_type_id is not None:
            out.append(("vehicle", str(self.vehicle_type_id)))
        if self.contact_id is not None:
            out.append(("customer", str(self.contact_id)))
        if self.group_key:
            out.append(("group", self.group_key))
        if self.coverage:
            out.append(("f", self.coverage))
        return out

    def _params_for(self, view: str, anchor: date, start: date, end: date) -> dict[str, str]:
        # date params first so URLs read `?view=week&day=…&vehicle=…` — stable order.
        pairs: list[tuple[str, str]] = []
        if view != "day":
            pairs.append(("view", view))
        if view == "range":
            pairs += [("start", start.isoformat()), ("end", end.isoformat())]
        else:
            pairs.append(("day", anchor.isoformat()))
        pairs += self._filter_params()
        return dict(pairs)

    def _url_for(self, view: str, anchor: date, start: date, end: date) -> str:
        return "?" + urlencode(self._params_for(view, anchor, start, end))

    def prev_params(self) -> dict[str, str]:
        return self._shift(-1)

    def next_params(self) -> dict[str, str]:
        return self._shift(1)

    def _shift(self, direction: int) -> dict[str, str]:
        if self.view == "day":
            step = timedelta(days=direction)
        elif self.view == "week":
            step = timedelta(days=7 * direction)
        else:  # range — move the whole window by its own length
            step = timedelta(days=self.span_days * direction)
        return self._params_for(self.view, self.anchor + step, self.start + step, self.end + step)

    def today_params(self) -> dict[str, str]:
        today = timezone.localdate()
        end = today + (self.end - self.start) if self.view == "range" else today
        return self._params_for(self.view, today, today, end)

    @property
    def prev_url(self) -> str:
        return "?" + urlencode(self.prev_params())

    @property
    def next_url(self) -> str:
        return "?" + urlencode(self.next_params())

    @property
    def today_url(self) -> str:
        return "?" + urlencode(self.today_params())

    def switch_url(self, view: str) -> str:
        """URL for the same anchor date shown in another view."""
        return self._url_for(view, self.anchor, self.start, self.end)

    def _current_params(self) -> dict[str, str]:
        return self._params_for(self.view, self.anchor, self.start, self.end)

    def without_url(self, *drop: str) -> str:
        """The current board URL with one or more filter params removed (chip dismiss)."""
        keep = {k: v for k, v in self._current_params().items() if k not in drop}
        return "?" + urlencode(keep)

    def coverage_url(self, bucket: str) -> str:
        """Toggle a coverage bucket in the attention strip — click the active one to clear."""
        params = {k: v for k, v in self._current_params().items() if k != "f"}
        if self.coverage != bucket:
            params["f"] = bucket
        return "?" + urlencode(params)
