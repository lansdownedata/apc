"""Client-approved touch-point copy + rendering context.

Source: docs/touchpoints/'Touch Points & Communicationsv2.pdf' (LQC program TP1-TP8).
Copy is verbatim client copy — do not rewrite; %TOKENS% became {format} keys.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings


@dataclass(frozen=True)
class TouchPointTemplate:
    kind: str
    # lead_created | quote_sent | quote_viewed | quote_expires | trips_done |
    # balance_due | reservation_pickup | triggered
    anchor: str
    offset: timedelta
    channels: tuple[str, ...]
    subject: str = ""
    email_body: str = ""
    sms_body: str = ""
    # Who the message goes to. "customer" -> lead.contact; "affiliate" -> the confirmed
    # Assignment's vendor. Only the reservation-anchored kinds use "affiliate".
    audience: str = "customer"


TEMPLATES: dict[str, TouchPointTemplate] = {
    "tp1_welcome": TouchPointTemplate(
        kind="tp1_welcome",
        anchor="lead_created",
        offset=timedelta(minutes=30),
        channels=("email", "sms"),
        subject="Thank you for your interest in {company_name}",
        email_body=(
            "{first_name},\n"
            "Thank you for taking the time to visit our website and providing details "
            "about your upcoming transportation needs. We are very excited to earn your "
            "business.\n\n"
            "Pick-up: {pickup_address} {pickup_time} {pickup_date}\n"
            "Drop-off: {dropoff_address}\n\n"
            "{quote_breakdown}\n\n"
            "If you haven't received a quote from us via email, then we need additional "
            "information from you to properly provide a quote. Please reach out to us at "
            "{company_phone} when convenient for you.\n\n"
            "We can discuss your trip details to make sure we fully understand your needs "
            "and help you select the best options.\n\n"
            "We look forward to serving you and hope you have a wonderful day!"
        ),
        sms_body=(
            "{first_name}, it's {company_name}. Thank you for taking the time to visit "
            "our website and providing details about your upcoming transportation needs. "
            "We are very excited to earn your business.\n\n"
            "Pick-up: {pickup_address} {pickup_time} {pickup_date}\n\n"
            "If you haven't received a quote from us via email, then we need additional "
            "information from you to properly provide a quote. Please reach out to us at "
            "{company_phone} when convenient for you."
        ),
    ),
    "tp2_lead_followup": TouchPointTemplate(
        kind="tp2_lead_followup",
        anchor="lead_created",
        offset=timedelta(hours=2),
        channels=("email",),
        subject="Any questions about your transportation request?",
        email_body=(
            "{first_name},\n"
            "Do you have any questions about your request for transportation from "
            "{pickup_address} at {pickup_time}?\n\n"
            "As a reminder we've included a breakdown:\n{quote_breakdown}\n\n"
            "Please feel free to reach out to us should you need any assistance.\n\n"
            "{company_name}\n{company_phone}\n{company_email}"
        ),
    ),
    "tp3_quote_sent_sms": TouchPointTemplate(
        kind="tp3_quote_sent_sms",
        anchor="quote_sent",
        offset=timedelta(minutes=3),
        channels=("sms",),
        sms_body=(
            "Hey, {first_name}! This is {agent_first} with {company_name}. We just "
            "emailed you the quote you requested for {pickup_date}. It should be in your "
            "inbox at any moment. If you'd prefer to book now, you can do so right here: "
            "{quote_link} Please let me know if you have any questions."
        ),
    ),
    "tp4_viewed_sms": TouchPointTemplate(
        kind="tp4_viewed_sms",
        anchor="quote_viewed",
        offset=timedelta(minutes=20),
        channels=("sms",),
        sms_body=(
            "{first_name}, do you have any questions about your quote? {quote_link} "
            "Please give us a call at {company_phone} or send us an email at "
            "{company_email} if so.\n\nBest wishes, {company_name}"
        ),
    ),
    "tp5_viewed_email": TouchPointTemplate(
        kind="tp5_viewed_email",
        anchor="quote_viewed",
        offset=timedelta(hours=2),
        channels=("email",),
        subject="We have noticed you haven't booked your quote",
        email_body=(
            "Do you have any questions about your request for transportation from "
            "{pickup_address} at {pickup_time}?\n\n"
            "As a reminder we've included a breakdown:\n{quote_breakdown}\n\n"
            "Book your quote here: {quote_link}"
        ),
    ),
    "tp6_quote_followup": TouchPointTemplate(
        kind="tp6_quote_followup",
        anchor="quote_sent",
        offset=timedelta(hours=24),
        channels=("email",),
        subject="Following up on your {company_name} quote",
        email_body=(
            "{first_name}, We wanted to follow up on the quote that we sent you "
            "yesterday. If you have any questions or would like to discuss trip details, "
            "please reply to this email.\n\n"
            "{quote_breakdown}\n\n"
            "Book now: {quote_link}\n\n"
            "{company_name}\n{company_phone}\n{company_email}"
        ),
    ),
    "tp7_expiring": TouchPointTemplate(
        kind="tp7_expiring",
        anchor="quote_expires",
        offset=timedelta(hours=-24),
        channels=("email", "sms"),
        subject="Your quote is expiring tomorrow",
        email_body=(
            "{first_name},\n"
            "We hope you're doing well! We wanted to let you know that your quote is "
            "scheduled to expire tomorrow if it hasn't been reserved yet.\n\n"
            "If you have any questions, need additional information, or would like "
            "assistance moving forward, please feel free to reach out. We're always "
            "happy to help.\n\n"
            "If you like, I can:\n"
            "- Update any details or adjust the scope to better fit your needs\n"
            "- Answer any questions you have about the pricing, options or timeline.\n\n"
            "Book here before it expires: {quote_link}\n\n"
            "{company_name} {company_phone}"
        ),
        sms_body=(
            "Hi, {first_name}, I wanted to give you a heads up that your quote is "
            "expiring tomorrow. Please review the email we just sent you for more "
            "information and options.\n\nRegards, {company_name} {company_phone}"
        ),
    ),
    "tp8_expired": TouchPointTemplate(
        kind="tp8_expired",
        anchor="quote_expires",
        offset=timedelta(hours=24),
        channels=("sms",),
        sms_body=(
            "{first_name}, We just wanted to check in with you to see if you still need "
            "transportation for {pickup_date}. If you'd like a fresh quote, reply here "
            "or call us at {company_phone} and we'll get one right over: {quote_link}"
        ),
    ),
    "review_request": TouchPointTemplate(
        kind="review_request",
        anchor="trips_done",
        offset=timedelta(hours=2),
        channels=("sms",),
        sms_body=(
            "Hi {first_name}, thank you for riding with {company_name}! We'd love to "
            "hear how everything went — would you take a moment to leave us a review? "
            "{review_link}"
        ),
    ),
    # DRAFT COPY — awaiting client sign-off. Unlike TP1–TP8 this is NOT client-verbatim
    # (the touch-points PDF has no payment-reminder message). Do not treat as approved.
    "payment_reminder": TouchPointTemplate(
        kind="payment_reminder",
        anchor="balance_due",  # midnight on PaymentPlan.balance_due_date
        offset=timedelta(hours=-72),
        channels=("sms", "email"),
        subject="Your remaining balance for reservation {quote_no}",
        email_body=(
            "{first_name},\n\n"
            "This is a friendly reminder that the remaining balance of {balance_amount} for "
            "reservation {quote_no} is scheduled to be charged on {balance_due_date} to the "
            "card ending {card_last4}.\n\n"
            "No action is needed — we'll take care of it automatically. If anything has "
            "changed, or you'd like to use a different card, reply here or call us at "
            "{company_phone}.\n\n"
            "You can review your reservation and payment here: {pay_link}\n\n"
            "Thank you for choosing {company_name}."
        ),
        sms_body=(
            "{first_name}, it's {company_name}. The remaining balance of {balance_amount} for "
            "reservation {quote_no} will be charged on {balance_due_date} to the card ending "
            "{card_last4}. Nothing to do — reply here if anything's changed. {pay_link}"
        ),
    ),
    # ---------------------------------------------------------------------------------
    # Reservation-lifecycle messaging (APC-18-22). Every body below is DRAFT starting
    # copy from docs/touchpoints/2026-09-04-APC-18-22-draft-copy.md, NOT client-verbatim.
    # CLIENT COPY PENDING (APC-27) — the client's final wording replaces each of these
    # before anything ships. Tests assert structure / variables, never the wording.
    # ---------------------------------------------------------------------------------
    "wed_final_details": TouchPointTemplate(
        kind="wed_final_details",
        anchor="reservation_pickup",
        offset=timedelta(days=-7),
        channels=("email", "sms"),
        subject="Final details for your {company_name} wedding transportation",
        email_body=(
            "{first_name},\n\n"
            "Your wedding transportation is booked for {trip_pickup_date} — we're looking "
            "forward to it.\n\n"
            "About a week out, we ask every couple for two last things so the day runs "
            "smoothly:\n\n"
            "  1. Your day-of point of contact — the name and cell number of whoever we "
            "should call if plans shift on the day (often a planner or a member of the "
            "wedding party, not the couple).\n"
            "  2. The wedding name as you'd like it to appear on our dispatch and driver "
            "notes.\n\n"
            "Please send those here: {confirm_link}\n\n"
            "If anything about the schedule has changed, this is the perfect time to tell "
            "us.\n\n"
            "{company_name}\n{company_phone}"
        ),
        sms_body=(
            "{first_name}, it's {company_name}. Your wedding transportation is "
            "{trip_pickup_date}. We need two last details: your day-of point of contact "
            "(name + cell) and the wedding name for our notes. Send them here: {confirm_link}"
        ),
    ),
    "trip_confirm_customer": TouchPointTemplate(
        kind="trip_confirm_customer",
        anchor="reservation_pickup",
        offset=timedelta(hours=-72),
        channels=("email", "sms"),
        subject="Please confirm your trip — {company_name} {quote_no}",
        email_body=(
            "{first_name},\n\n"
            "Your travel with us is coming up. Please take a moment to confirm the details "
            "below are correct.\n\n"
            "{trip_sheet}\n\n"
            "  We'll reach you at: {contact_on_file}\n\n"
            "Confirm: {confirm_link}\n\n"
            "If anything is wrong, or your plans have changed, reply to this email or call "
            "us right away at {company_phone} — the sooner we know, the better we can "
            "adjust.\n\n"
            "{company_name}"
        ),
        sms_body=(
            "{first_name}, it's {company_name}. Your trip on {trip_pickup_when} is coming "
            "up. Please confirm the details and let us know of any changes: {confirm_link}"
        ),
    ),
    # Second wave of the same acknowledgement. Sent only when the day is still
    # unconfirmed; at T-24h an unconfirmed day moves to the daily office report instead
    # (APC-19), so this is the last thing the customer is asked automatically.
    "trip_confirm_customer_2": TouchPointTemplate(
        kind="trip_confirm_customer_2",
        anchor="reservation_pickup",
        offset=timedelta(hours=-48),
        channels=("email", "sms"),
        subject="Still need your confirmation — {company_name} {quote_no}",
        email_body=(
            "{first_name},\n\n"
            "We haven't heard back on the details for your upcoming travel, and we'd rather "
            "check than assume. Please take a moment to confirm:\n\n"
            "{trip_sheet}\n\n"
            "  We'll reach you at: {contact_on_file}\n\n"
            "Confirm: {confirm_link}\n\n"
            "If anything is wrong, or your plans have changed, reply to this email or call "
            "us right away at {company_phone}.\n\n"
            "{company_name}"
        ),
        sms_body=(
            "{first_name}, it's {company_name}. We still need your confirmation for your "
            "travel on {trip_pickup_when}. Please take a look: {confirm_link}"
        ),
    ),
    "trip_confirm_affiliate": TouchPointTemplate(
        kind="trip_confirm_affiliate",
        anchor="reservation_pickup",
        offset=timedelta(hours=-48),
        channels=("email",),
        audience="affiliate",
        subject="Confirm coverage — {company_name} trip {trip_pickup_date}",
        email_body=(
            "{vendor_name},\n\n"
            "Please confirm you're covering this trip for {company_name}.\n\n"
            "{trip_sheet}\n\n"
            "Payout: {payout}\n\n"
            "Confirm here: {ack_link}\n\n"
            "If you can no longer cover this trip, tell us now by replying or calling "
            "{company_phone} so we can arrange a backup.\n\n"
            "{company_name}\n{company_phone}"
        ),
    ),
    "driver_released": TouchPointTemplate(
        kind="driver_released",
        anchor="triggered",
        offset=timedelta(0),
        channels=("email", "sms"),
        subject="Your driver for {trip_pickup_date} — {company_name}",
        email_body=(
            "{first_name},\n\n"
            "Here are your driver and vehicle details for {trip_pickup_when}:\n\n"
            "  Driver:  {driver_name}\n"
            "  Cell:    {driver_cell}\n"
            "  Vehicle: {vehicle_description} ({vehicle_number})\n\n"
            "Your driver will contact you as pickup approaches. If you need anything before "
            "then, call us at {company_phone}.\n\n"
            "{company_name}"
        ),
        sms_body=(
            "{first_name}, it's {company_name}. Your driver for {trip_pickup_when}: "
            "{driver_name}, cell {driver_cell}, {vehicle_description} ({vehicle_number}). "
            "They'll reach out as pickup nears."
        ),
    ),
    "status_dispatched": TouchPointTemplate(
        kind="status_dispatched",
        anchor="triggered",
        offset=timedelta(0),
        channels=("email", "sms"),
        subject="Your trip is confirmed and assigned — {company_name}",
        email_body=(
            "{first_name},\n\n"
            "Your trip is confirmed and assigned for {trip_pickup_when}.\n\n"
            "  Driver:  {driver_name}\n"
            "  Cell:    {driver_cell}\n"
            "  Vehicle: {vehicle_description} ({vehicle_number})\n\n"
            "Full details: {confirm_link}\n\n"
            "{company_name}"
        ),
        sms_body=(
            "{first_name}, it's {company_name}. Your trip is confirmed and assigned for "
            "{trip_pickup_when}. Driver: {driver_name}, {vehicle_description}. Full "
            "details: {confirm_link}"
        ),
    ),
    "status_on_the_way": TouchPointTemplate(
        kind="status_on_the_way",
        anchor="triggered",
        offset=timedelta(0),
        channels=("sms",),
        sms_body=(
            "{first_name}, your {company_name} driver is on the way to your "
            "{trip_pickup_time} pickup."
        ),
    ),
    "status_arrived": TouchPointTemplate(
        kind="status_arrived",
        anchor="triggered",
        offset=timedelta(0),
        channels=("sms",),
        sms_body=("{first_name}, your {company_name} driver has arrived at the pickup location."),
    ),
    # CLIENT COPY PENDING (APC-27). The one message here that has to do real work: the
    # customer believes they paid days ago. It has to say plainly that nothing was taken,
    # why, and what to do — apologetically, without inviting a chargeback dispute over a
    # charge that never existed.
    "order_auth_expired": TouchPointTemplate(
        kind="order_auth_expired",
        anchor="triggered",
        offset=timedelta(0),
        channels=("email", "sms"),
        subject="Your {company_name} booking needs a new payment authorization",
        email_body=(
            "{first_name},\n\n"
            "We're sorry — we weren't able to confirm your trip in time, and your bank has "
            "now released the hold on your card.\n\n"
            "**No money was taken.** The hold has come off your account, and there is "
            "nothing for you to cancel or dispute.\n\n"
            "Your quote {quote_no} is still good and we'd still like to take the trip. To "
            "book it, please authorize again here: {pay_link}\n\n"
            "If anything about your plans has changed, or you'd rather talk it through, "
            "call us at {company_phone} — we'd rather hear from you than lose the booking.\n\n"
            "{company_name}"
        ),
        sms_body=(
            "{first_name}, it's {company_name}. We couldn't confirm your trip in time and "
            "your bank released the hold on your card — no money was taken. Your quote "
            "{quote_no} is still good; you can authorize again here: {pay_link} Or call "
            "us at {company_phone}."
        ),
    ),
}


def _trip_context(reservation) -> dict[str, str]:
    """Per-trip vars for the reservation-anchored kinds (APC-18-22).

    Always returns every key; blank when there's no reservation or the datum is missing.
    Times render in the trip's own timezone with its abbreviation (CLAUDE.md rules) — never
    the viewer's.
    """
    keys = (
        "trip_pickup_date",
        "trip_pickup_time",
        "trip_pickup_tz",
        "trip_pickup_when",
        "trip_routing",
        "trip_passengers",
        "trip_vehicle",
        "trip_sheet",
        "contact_on_file",
        "vendor_name",
        "payout",
        "driver_name",
        "driver_cell",
        "vehicle_description",
        "vehicle_number",
    )
    ctx = dict.fromkeys(keys, "")
    if reservation is None:
        return ctx

    pickup_at = reservation.pickup_at
    if pickup_at is not None:
        ctx["trip_pickup_date"] = f"{pickup_at:%b %d, %Y}"
        if reservation.pickup_time is not None:
            # Include the abbreviation on every rendered time, per CLAUDE.md — a
            # customer or affiliate outside the trip's own zone must never see a bare
            # clock time and have to guess which zone it's in.
            tz = reservation.pickup_tz_abbrev
            ctx["trip_pickup_tz"] = tz
            ctx["trip_pickup_time"] = f"{pickup_at:%-I:%M %p} {tz}".strip()
            ctx["trip_pickup_when"] = f"{ctx['trip_pickup_date']} at {ctx['trip_pickup_time']}"
        else:
            # No pickup time on the trip — "{date} at {time}" would render a dangling
            # "at " with nothing after it, so the combined var drops the "at" clause too.
            ctx["trip_pickup_when"] = ctx["trip_pickup_date"]

    # `.stops.all()` (not `.ordered_stops`, which always re-queries — see the repo's
    # `pickup`/`dropoff` gotcha) so a caller that prefetched "reservation__stops" (e.g.
    # `run_touchpoints`) pays no extra query here. `Stop.Meta.ordering` is already
    # sequence, so the prefetched cache comes back in the right order.
    stops = list(reservation.stops.all())
    ctx["trip_routing"] = " → ".join(s.address for s in stops if s.address)
    ctx["trip_passengers"] = str(reservation.passengers)
    ctx["trip_vehicle"] = (reservation.vehicle.name if reservation.vehicle else "") or (
        reservation.service_type.name if reservation.service_type else ""
    )

    lead = reservation.lead
    contact = lead.contact
    phone = (contact.phone or "").strip()
    ctx["contact_on_file"] = f"{contact.name} · {phone}".strip(" ·") if contact.name else phone

    from apps.dispatch.selectors import confirmed_assignment

    confirmed = confirmed_assignment(reservation)
    if confirmed is not None:
        if confirmed.vendor_id:
            ctx["vendor_name"] = confirmed.vendor.contact_name or confirmed.vendor.name
        ctx["payout"] = f"${confirmed.payout:,.2f}"
        info = confirmed.driver_info
        if info:
            ctx["driver_name"] = info["name"] or ""
            ctx["driver_cell"] = info["cell"] or ""
            ctx["vehicle_description"] = info["vehicle_desc"] or ""
            ctx["vehicle_number"] = info["vehicle_number"] or ""

    from apps.reservations.services import trip_sheet_text

    ctx["trip_sheet"] = trip_sheet_text(reservation)
    return ctx


def build_context(lead, reservation=None) -> dict[str, str]:
    """Build rendering context from a lead and its relationships.

    All keys are always present; missing values are empty strings. Pass ``reservation``
    for the per-trip kinds (APC-18-22) to fill the ``trip_*`` / driver / affiliate vars.
    """
    contact = lead.contact
    first = (contact.name or "").split(" ")[0] or "there"
    agent = lead.assigned_agent
    agent_first = (
        (agent.get_full_name() or agent.get_username()).split(" ")[0] if agent else "the team"
    )
    reservations = list(lead.reservations.all())
    first_res = reservations[0] if reservations else None
    stops = list(first_res.stops.all()) if first_res else []

    def format_breakdown(r):
        vehicle = r.vehicle.name if r.vehicle else "Vehicle TBD"
        passengers = f"{r.passengers} passengers"
        total = f"${r.line_total:,.2f}"
        if r.pickup_date and r.pickup_time:
            date_fmt = f"{r.pickup_date:%b %d}"
            time_fmt = f"{r.pickup_time:%-I:%M %p}"
            return f"{date_fmt} · {time_fmt} — {vehicle} · {passengers} — {total}"
        return f"{vehicle} · {passengers} — {total}"

    breakdown = "\n".join(format_breakdown(r) for r in reservations)
    pickup_date = (
        f"{first_res.pickup_date:%b %d, %Y}" if first_res and first_res.pickup_date else ""
    )
    pickup_time = (
        f"{first_res.pickup_time:%-I:%M %p}" if first_res and first_res.pickup_time else ""
    )
    plan = getattr(lead, "payment", None)
    balance_due = plan.balance_due_date if plan else None
    return {
        "first_name": first,
        "agent_first": agent_first,
        "company_name": settings.COMPANY_NAME,
        "company_phone": settings.COMPANY_PHONE,
        "company_email": settings.COMPANY_EMAIL,
        "pickup_date": pickup_date,
        "pickup_time": pickup_time,
        "pickup_address": stops[0].address if stops else "",
        "dropoff_address": stops[-1].address if len(stops) > 1 else "",
        "quote_no": lead.quote_no,
        "quote_breakdown": breakdown,
        "quote_link": "",  # filled by callers that have request/base-url context
        "pay_link": "",  # filled by the payment_reminder sender
        "review_link": "",  # filled by the review_request sender
        "confirm_link": "",  # filled by the reservation-anchored senders
        "ack_link": "",  # filled by the affiliate-confirmation sender
        "balance_amount": f"${plan.balance_amount:,.2f}" if plan else "",
        "balance_due_date": f"{balance_due:%b %d, %Y}" if balance_due else "",
        "card_last4": plan.card_last4 if plan else "",
        **_trip_context(reservation),
    }


def render(text: str, ctx: dict[str, str]) -> str:
    """Render a template string with the context dict using str.format."""
    return text.format(**ctx)
