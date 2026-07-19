"""Contacts directory — customer list with lifetime value, trips, and last activity."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Count, DecimalField, Max, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.phone import to_e164
from apps.leads.models import Lead
from apps.payments.models import PaymentPlan

from .models import Contact, ContactPhone

# Sentinel so contacts with no activity sort last (None isn't orderable against datetimes).
_NO_ACTIVITY = datetime.min.replace(tzinfo=UTC)
_LTV_FIELD = DecimalField(max_digits=12, decimal_places=2)

# Label choices for the phone-numbers block's searchable select (never a bare <select>).
PHONE_LABELS = [("mobile", "Mobile"), ("work", "Work"), ("home", "Home")]


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
        # the digits of any of the contact's numbers via a pk subquery (see
        # `ContactPhone.objects.matching`), so this OR can't fan out the `contacts`
        # queryset's own aggregate annotations, and non-phone-like queries ("Suite 5")
        # don't collapse to a digit and over-match.
        phone_matches = ContactPhone.objects.matching(query).values("contact_id")
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


@login_required
def contact_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Contact record page. Skeleton + phones only — see the scope boundary in the plan.

    LTV/order-history/inline-edit/contact_create belong to the (approved but not yet
    built) record-pages work; adding them here would be rework, not a bonus.
    """
    contact = get_object_or_404(Contact.objects.prefetch_related("phones"), pk=pk)
    return render(
        request,
        "contacts/contact_detail.html",
        {
            "nav": "contacts",
            "page_title": contact.name,
            "contact": contact,
            "phone_labels": PHONE_LABELS,
        },
    )


@login_required
@require_POST
def contact_phone_add(request: HttpRequest, pk: int) -> HttpResponse:
    """Attach a new number to a contact.

    Distinguishes the two reasons `add_phone` can return None — reusing the
    two-message pattern from `apps.leads.views.lead_update` — so an agent isn't told
    a number is malformed when it's really just claimed by another contact.
    """
    contact = get_object_or_404(Contact, pk=pk)
    phone_raw = request.POST.get("phone", "")
    label = request.POST.get("label", "").strip()

    if to_e164(phone_raw) is None:
        return JsonResponse({"ok": False, "error": "Enter a valid phone number."}, status=400)

    phone = contact.add_phone(phone_raw, label=label)
    if phone is None:
        return JsonResponse(
            {"ok": False, "error": "That number is already assigned to another contact."},
            status=400,
        )
    return redirect("contact_detail", pk=contact.pk)


@login_required
@require_POST
def contact_phone_primary(request: HttpRequest, pk: int, phone_pk: int) -> HttpResponse:
    """Promote one of a contact's existing numbers to primary, demoting the current one."""
    contact = get_object_or_404(Contact, pk=pk)
    phone = get_object_or_404(ContactPhone, pk=phone_pk, contact=contact)
    contact.set_primary_phone(phone.e164)
    return redirect("contact_detail", pk=contact.pk)


@login_required
@require_POST
def contact_phone_delete(request: HttpRequest, pk: int, phone_pk: int) -> HttpResponse:
    """Remove a number from a contact.

    Scoped by `contact=contact` so acting on another contact's phone 404s instead of
    silently succeeding (object-level authz — see the plan's note on this).
    """
    contact = get_object_or_404(Contact, pk=pk)
    get_object_or_404(ContactPhone, pk=phone_pk, contact=contact).delete()
    return redirect("contact_detail", pk=contact.pk)
