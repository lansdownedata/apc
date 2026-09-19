"""Wedding intake: what a couple told us, kept whole and read back by category.

The first version of this flow (spec 2026-08-30) derived the day's trips from the answers.
It was retired on 2026-09-19: weddings run too many different ways for a rule to guess the
legs, and the generated days kept missing movements. The customer now answers the same
questions, we confirm every detail back to them, and one of our own people builds the
trips in the workspace.

So this module no longer generates anything. It owns the vocabulary of the intake — the
groups, the limits, a `Site` — and `wedding_answers`, the single rendering of "each
category, its questions, and the answers given" that the confirmation page, the
confirmation email, the lead's notes and the office's details card all read.

Deliberately pure — no models, no request, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time

GROUP_GUESTS = "guests"
GROUP_PARTY = "party"
GROUP_FAMILY = "family"
GROUP_COUPLE = "couple"
GROUPS = (GROUP_GUESTS, GROUP_PARTY, GROUP_FAMILY, GROUP_COUPLE)
GROUP_LABELS = {
    GROUP_GUESTS: "Our guests",
    GROUP_PARTY: "The wedding party",
    GROUP_FAMILY: "Family & VIPs",
    GROUP_COUPLE: "Just the two of us",
}
# The headcount question each riding group is asked, keyed to its payload field. The
# couple is always two, so it has no count.
GROUP_COUNTS = (
    (GROUP_GUESTS, "guest_count", "Guests riding the shuttle"),
    (GROUP_PARTY, "party_count", "Wedding party"),
    (GROUP_FAMILY, "family_count", "Family & VIPs"),
)
# Only these two groups are collected from a hotel, so only they are asked about one.
HOTEL_GROUPS = (GROUP_GUESTS, GROUP_FAMILY)

MIN_PASSENGERS = 1
MAX_PASSENGERS = 400
MAX_NOTES = 2000

HOTELS_TBD_ANSWER = "Not booked yet"
TIMES_TBD_ANSWER = "Not set yet"
UNANSWERED = "—"


@dataclass(frozen=True)
class Site:
    """A place the wedding touches — a directory Venue, or a name the couple typed.

    `venue_id` is set only for a curated directory row; a free-typed or LocationIQ site
    keeps its name and whatever address came back, which is all a Stop needs when the
    office builds the trips.
    """

    name: str
    sub: str = ""
    city: str = ""
    state: str = ""
    address: str = ""
    latitude: str | None = None
    longitude: str | None = None
    vehicle_cap: int | None = None
    cap_note: str = ""
    venue_id: int | None = None

    @property
    def line(self) -> str:
        """The display sub-line: whatever the directory gave us, else town and state."""
        return self.sub or ", ".join(p for p in (self.address, self.city, self.state) if p)


# Weddings inside this many days are the ones that go cold fastest (11% of inquiries
# arrive inside a month) — they raise a Lead alert so the pipeline surfaces them today.
ALERT_WINDOW_DAYS = 45


def is_time_sensitive(wedding_date: date, today: date) -> bool:
    """True for a wedding inside the alert window — a past date included, because a
    typo'd year is still something the office has to phone about."""
    return (wedding_date - today).days < ALERT_WINDOW_DAYS


def format_clock(value: time) -> str:
    """ "3:00 PM" — no leading zero, no platform-specific strftime directive."""
    hour = value.hour % 12 or 12
    return f"{hour}:{value.minute:02d} {'PM' if value.hour >= 12 else 'AM'}"


def format_long_date(value: date) -> str:
    """ "Saturday, October 16, 2027" — the weekday matters; couples check it."""
    return f"{value:%A, %B} {value.day}, {value.year}"


def is_wedding_payload(payload: dict | None) -> bool:
    """True for a `Lead.intake_payload` the wedding intake wrote.

    The column is a generic inbound-request archive — a Calendly booking lands in it too —
    so "non-empty" is not the test.
    """
    return bool(payload) and "wedding_date" in payload and "groups" in payload


def blank_payload() -> dict:
    """What a wedding the office starts from New wedding holds before anyone has answered.

    It is what makes the lead a wedding from its first second: the details card and the
    way back into Edit details must not depend on the agent finishing a first save.
    """
    return {"wedding_date": "", "groups": [], "same_site": True}


def _clock(hhmm: str) -> str:
    try:
        hour, _, minute = hhmm.partition(":")
        return format_clock(time(int(hour), int(minute)))
    except (TypeError, ValueError):
        return TIMES_TBD_ANSWER


def _long_date(iso: str | None) -> str:
    try:
        return format_long_date(date.fromisoformat(iso or ""))
    except (TypeError, ValueError):
        return UNANSWERED


def wedding_answers(payload: dict | None) -> list[dict]:
    """Every question the intake asked, with the answer given, grouped by category.

    Returns `[{"title": str, "rows": [(question, answer), ...]}]`, or `[]` when the payload
    is not a wedding. A question the couple was never shown is left out rather than shown
    blank: a count for a group that isn't riding is a stepper default, not an answer, and
    nobody is asked about hotels when no one rides from one. "Not yet" *is* an answer and
    is reported as one — it is what stops an agent quoting a guess as if it were confirmed.
    """
    if not is_wedding_payload(payload):
        return []
    groups = [g for g in GROUPS if g in (payload.get("groups") or [])]
    same_site = bool(payload.get("same_site"))

    venue_rows = [
        ("Reception venue", payload.get("venue_name") or UNANSWERED),
        ("Ceremony at the same place?", "Yes" if same_site else "No — two locations"),
    ]
    if not same_site:
        ceremony = payload.get("ceremony") or {}
        venue_rows.append(("Ceremony location", ceremony.get("name") or UNANSWERED))

    riding_rows = [("Who needs a ride?", ", ".join(GROUP_LABELS[g] for g in groups) or UNANSWERED)]
    riding_rows += [
        (label, str(payload[field]))
        for group, field, label in GROUP_COUNTS
        if group in groups and payload.get(field)
    ]

    categories = [
        {"title": "Date", "rows": [("Wedding date", _long_date(payload.get("wedding_date")))]},
        {"title": "Venue & ceremony", "rows": venue_rows},
        {"title": "Who's riding", "rows": riding_rows},
    ]
    if any(g in HOTEL_GROUPS for g in groups):
        hotels = "; ".join(h.get("name", "") for h in payload.get("hotels") or [])
        if payload.get("hotels_tbd") or not hotels:
            hotels = HOTELS_TBD_ANSWER
        categories.append({"title": "Hotels", "rows": [("Where is everyone staying?", hotels)]})

    times_tbd = bool(payload.get("times_tbd"))
    categories.append(
        {
            "title": "Times",
            "rows": [
                (
                    "Ceremony starts",
                    TIMES_TBD_ANSWER if times_tbd else _clock(payload.get("ceremony_time") or ""),
                ),
                (
                    "Venue requires everyone out by",
                    TIMES_TBD_ANSWER if times_tbd else _clock(payload.get("end_time") or ""),
                ),
            ],
        }
    )
    categories.append(
        {
            "title": "Anything else",
            "rows": [
                (
                    "Anything else we should know?",
                    (payload.get("notes") or "").strip() or UNANSWERED,
                )
            ],
        }
    )
    return categories


def venue_cap_line(payload: dict | None) -> str:
    """ "Venue vehicle cap: 40 passengers" when the venue limits what fits, else "".

    Not an answer the couple gave — it comes off our own directory row — but it is the
    first thing an agent building the trips by hand has to know, so it rides with them.
    """
    venue = (payload or {}).get("venue") or {}
    cap, note = venue.get("vehicle_cap"), (venue.get("cap_note") or "").strip()
    if not cap:
        return ""
    return f"Venue vehicle cap: {cap} passengers" + (f" — {note}" if note else "")


def build_notes(payload: dict) -> str:
    """The block the pipeline card and an agent skimming the lead read first.

    The `!!` lines are the point of the format: they are what stops someone quoting an
    unconfirmed detail as if the customer had given it.
    """
    lines = [f"WEDDING — {_long_date(payload.get('wedding_date'))}"]
    for category in wedding_answers(payload)[1:]:
        lines += [f"{question}: {answer}" for question, answer in category["rows"]]
    cap = venue_cap_line(payload)
    if cap:
        lines.append(cap)
    if payload.get("times_tbd"):
        lines.append("!! Times NOT SET — customer had no timeline yet. Confirm before quoting.")
    if payload.get("hotels_tbd"):
        lines.append(
            "!! Hotels NOT BOOKED — guest pickup points unconfirmed. Confirm before quoting."
        )
    return "\n".join(lines)
