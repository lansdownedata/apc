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

from .models import Contact, ContactPhone

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
        # `phone` isn't a concrete field anymore (ContactPhone table) — match against
        # the digits of any of the contact's numbers via a pk subquery, so this OR
        # can't fan out the `contacts` queryset's own aggregate annotations.
        digits = re.sub(r"\D", "", query)
        phone_matches = (
            ContactPhone.objects.filter(e164__icontains=digits).values("contact_id")
            if digits
            else ContactPhone.objects.none().values("contact_id")
        )
        contacts = contacts.filter(
            Q(name__icontains=query)
            | Q(company__icontains=query)
            | Q(pk__in=phone_matches)
            | Q(email__icontains=query)
        )

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
