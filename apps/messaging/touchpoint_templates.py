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
    anchor: str  # lead_created | quote_sent | quote_viewed | quote_expires | trips_done
    offset: timedelta
    channels: tuple[str, ...]
    subject: str = ""
    email_body: str = ""
    sms_body: str = ""


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
}


def build_context(lead) -> dict[str, str]:
    """Build rendering context from a lead and its relationships.

    All keys are always present; missing values are empty strings.
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
        "review_link": "",  # filled by the review_request sender
    }


def render(text: str, ctx: dict[str, str]) -> str:
    """Render a template string with the context dict using str.format."""
    return text.format(**ctx)
