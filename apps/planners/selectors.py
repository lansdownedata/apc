"""What a planner's pages read (APC-66/67). Plain dicts only: no model instance reaches a
planner template, so a price, a cost, an affiliate or an internal note cannot leak through
an attribute someone reaches for later — the fields simply aren't there."""

from __future__ import annotations

from datetime import date, time

from django.conf import settings
from django.db.models import Prefetch
from django.utils import timezone

from apps.core.phone import to_e164
from apps.core.templatetags.phone_filters import phone_display
from apps.leads.models import Lead, LeadContact
from apps.public.wedding import format_long_date
from apps.reservations.models import Reservation, today_at

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


def dashboard(lead: Lead) -> dict:
    """Everything the wedding page shows, for one wedding the planner holds a role on."""
    lead = Lead.objects.select_related("contact").prefetch_related("reservations").get(pk=lead.pk)
    lead.planner_roles = []
    return _summary(lead, now=timezone.now())


def access_for(contact) -> dict:
    """The contact profile's Planner access card (staff side): one line of state and which
    of Invite / Resend / Revoke apply. Two queries."""
    from django.utils import dateformat

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
