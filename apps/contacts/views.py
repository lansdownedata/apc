"""Contacts directory — customer list with lifetime value, trips, and last activity."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Count, DecimalField, Max, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from apps.leads.models import Lead
from apps.payments.models import PaymentPlan

from .models import Contact

# Sentinel so contacts with no activity sort last (None isn't orderable against datetimes).
_NO_ACTIVITY = datetime.min.replace(tzinfo=UTC)
_LTV_FIELD = DecimalField(max_digits=12, decimal_places=2)


@login_required
def contact_list(request: HttpRequest) -> HttpResponse:
    """Directory of contacts with SQL-computed LTV (booked plans only), trip count,
    and last-activity (latest lead or message). LTV uses a correlated Subquery so the
    trips Count join never multiplies the summed plan totals."""
    booked_ltv = (
        PaymentPlan.objects.filter(lead__contact=OuterRef("pk"), lead__status=Lead.Status.BOOKED)
        .values("lead__contact")
        .annotate(total=Sum("quote_total"))
        .values("total")
    )

    contacts = Contact.objects.annotate(
        trips=Count("leads__reservations", distinct=True),
        lifetime_value=Coalesce(
            Subquery(booked_ltv, output_field=_LTV_FIELD),
            Value(Decimal("0.00"), output_field=_LTV_FIELD),
        ),
        last_lead_at=Max("leads__updated_at"),
        last_message_at=Max("leads__messages__created_at"),
        latest_lead_id=Max("leads__id"),
    )

    query = request.GET.get("q", "").strip()
    if query:
        # Phones are stored E.164 (+16175559271); match on digits so a formatted
        # query like "(617) 555-9271" or "555-9271" still finds them.
        lookup = Q(name__icontains=query) | Q(company__icontains=query) | Q(email__icontains=query)
        phone_digits = re.sub(r"\D", "", query)
        if len(phone_digits) >= 3:
            lookup |= Q(phone__icontains=phone_digits)
        contacts = contacts.filter(lookup)

    rows = list(contacts)
    for c in rows:
        stamps = [d for d in (c.last_lead_at, c.last_message_at) if d is not None]
        c.last_activity = max(stamps) if stamps else None
    rows.sort(key=lambda c: c.last_activity or _NO_ACTIVITY, reverse=True)

    total_ltv = sum((c.lifetime_value for c in rows), Decimal("0.00"))

    return render(
        request,
        "contacts/contact_list.html",
        {
            "nav": "contacts",
            "page_title": "Contacts",
            "contacts": rows,
            "total_contacts": len(rows),
            "total_ltv": total_ltv,
            "q": query,
        },
    )
