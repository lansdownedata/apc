"""Contacts directory — customer list with lifetime value, trips, and last activity."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError
from django.db.models import (
    Count,
    DecimalField,
    Exists,
    Max,
    OuterRef,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.addresses.models import Address
from apps.addresses.smart_address import apply_posted_address
from apps.core.choices import Channel
from apps.core.phone import to_e164
from apps.leads.models import Lead
from apps.payments.models import PaymentPlan

from .models import Company, Contact

# Type-ahead results the modal shows at once — more than this is a search, not a pick.
SEARCH_LIMIT = 8


def _search_row(contact: Contact, leads: int) -> dict[str, object]:
    """One type-ahead row: exactly the fields the modal fills in, plus a trust signal."""
    return {
        "id": contact.pk,
        "name": contact.name,
        "company": contact.company.name if contact.company else "",
        "phone": contact.phone or "",
        "email": contact.email or "",
        "leads": leads,
    }


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
        last_message_at=Max("conversation__messages__created_at"),
        latest_lead_id=Max("leads__id"),
    )

    # Derived customer filter: a contact with no leads has no LTV, no trips, and nothing
    # the directory's columns are for. Every stranger who texts the main business number
    # becomes a Contact, so the default view would otherwise fill with wrong numbers.
    #
    # Exists, NOT .filter(leads__isnull=False) — the latter joins the leads table and
    # lists a three-quote customer three times. `trips` is already guarded by
    # distinct=True and LTV by its Subquery, so only the row set would be wrong.
    scope = request.GET.get("scope", "customers")
    if scope != "all":
        scope = "customers"
        contacts = contacts.filter(Exists(Lead.objects.filter(contact=OuterRef("pk"))))

    query = request.GET.get("q", "").strip()
    if query:
        # Same matching rule as the booking modal's type-ahead — see ContactManager.search.
        contacts = contacts.filter(pk__in=Contact.objects.search(query).values("pk"))

    rows = list(contacts)
    for c in rows:
        stamps = [d for d in (c.last_lead_at, c.last_message_at) if d is not None]
        c.last_activity = max(stamps) if stamps else None
    rows.sort(key=lambda c: c.last_activity or _NO_ACTIVITY, reverse=True)

    total_ltv = sum((c.lifetime_value for c in rows), Decimal("0.00"))
    company_names = [(co.name, co.name) for co in Company.objects.order_by("name")]

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
            "scope": scope,
            "scope_tabs": [("customers", "Customers"), ("all", "All contacts")],
            "channels": Channel.choices,
            "company_names": company_names,
        },
    )


@login_required
@require_GET
def contact_search(request: HttpRequest) -> JsonResponse:
    """Customer lookup for the New booking / New lead modal.

    `?q=` feeds the type-ahead dropdown. `?phone=&email=` answers "does this already
    exist?" for the inline hint — that arm goes through `find_match` so the E.164-vs-raw
    rules stay on the server instead of being re-implemented in the browser.
    """
    rows = (
        Contact.objects.search(request.GET.get("q", ""))
        .select_related("company")
        .annotate(lead_count=Count("leads", distinct=True))
        .order_by("name")[:SEARCH_LIMIT]
    )
    match = Contact.objects.find_match(
        phone=request.GET.get("phone", ""), email=request.GET.get("email", "")
    )
    return JsonResponse(
        {
            "results": [_search_row(c, c.lead_count) for c in rows],
            "match": _search_row(match, match.leads.count()) if match else None,
        }
    )


@login_required
@require_POST
def contact_create(request: HttpRequest) -> HttpResponse:
    """Add-contact modal target — dedupes by phone/email and resolves the company
    name string to a Company FK via `match_or_create`."""
    name = request.POST.get("name", "").strip()
    if not name:
        messages.error(request, "Name is required.")
        return redirect("contact_list")
    contact = Contact.objects.match_or_create(
        name=name,
        company_name=request.POST.get("company", ""),
        phone=request.POST.get("phone", ""),
        email=request.POST.get("email", ""),
        channel=request.POST.get("channel", Channel.WEBSITE),
    )
    messages.success(request, f"Contact {contact.name} saved.")
    return redirect("contact_detail", pk=contact.pk)


def _contact_stats(contact: Contact) -> dict[str, object]:
    """LTV / orders / trips for one contact — reuses the directory's booked-plan LTV rule."""
    ltv = PaymentPlan.objects.filter(
        lead__contact=contact, lead__status=Lead.Status.BOOKED
    ).aggregate(total=Sum("quote_total"))["total"] or Decimal("0.00")
    orders = contact.leads.filter(status=Lead.Status.BOOKED).count()
    trips = sum(lead.reservation_count for lead in contact.leads.all())
    return {"ltv": ltv, "orders": orders, "trips": trips}


@login_required
def contact_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Editable contact profile — header stats, contact-details card, order history."""
    contact = get_object_or_404(
        Contact.objects.select_related("company").prefetch_related("leads__reservations"), pk=pk
    )
    leads = list(
        contact.leads.select_related("payment")
        .prefetch_related("reservations")
        .annotate(trip_count=Count("reservations"))
        .order_by("-id")[:10]
    )
    company_names = [(co.name, co.name) for co in Company.objects.order_by("name")]
    return render(
        request,
        "contacts/contact_detail.html",
        {
            "nav": "contacts",
            "contact": contact,
            "stats": _contact_stats(contact),
            "leads": leads,
            "channels": Channel.choices,
            "company_names": company_names,
            "primary_addr_url": reverse("contact_address_update", args=[contact.pk, "primary"]),
            "billing_addr_url": reverse("contact_address_update", args=[contact.pk, "billing"]),
            "ac_url": reverse("integrations:geocode_autocomplete"),
        },
    )


@login_required
@require_POST
def contact_update(request: HttpRequest, pk: int) -> HttpResponse:
    """Partial-field autosave for the contact profile — validates phone/email/company."""
    contact = get_object_or_404(Contact, pk=pk)
    if "name" in request.POST and not request.POST.get("name", "").strip():
        return JsonResponse({"ok": False, "error": "Name cannot be blank."}, status=400)
    if "email" in request.POST:
        email = request.POST.get("email", "").strip()
        if email:
            try:
                validate_email(email)
            except ValidationError:
                return JsonResponse(
                    {"ok": False, "error": "Enter a valid email address."}, status=400
                )
    phone = None
    if "phone" in request.POST:
        raw = request.POST.get("phone", "").strip()
        if raw:
            phone = to_e164(raw)
            if phone is None:
                return JsonResponse(
                    {"ok": False, "error": "Enter a valid phone number."}, status=400
                )
        else:
            phone = ""

    fields = []
    for f in ("name", "notes"):
        if f in request.POST:
            setattr(contact, f, request.POST.get(f, "").strip())
            fields.append(f)
    if "email" in request.POST:
        contact.email = request.POST.get("email", "").strip()
        fields.append("email")
    if "channel" in request.POST and request.POST["channel"] in Channel.values:
        contact.channel = request.POST["channel"]
        fields.append("channel")
    if "company" in request.POST:
        contact.company = Company.objects.get_or_create_by_name(request.POST.get("company", ""))
        fields.append("company")
    if phone is not None:
        contact.phone = phone
        fields.append("phone")
    if "billing_same_as_primary" in request.POST:
        contact.billing_same_as_primary = request.POST["billing_same_as_primary"] == "true"
        fields.append("billing_same_as_primary")
    if fields:
        try:
            contact.save(update_fields=[*fields, "updated_at"])
        except IntegrityError:
            return JsonResponse(
                {"ok": False, "error": "That email is already used by another contact."},
                status=400,
            )
    return JsonResponse({"ok": True})


@login_required
def company_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Editable company profile — rolled-up LTV/orders/trips across its contacts."""
    company = get_object_or_404(Company.objects.select_related("billing_contact"), pk=pk)
    members = list(company.contacts.select_related("company").order_by("name"))
    ltv = PaymentPlan.objects.filter(
        lead__contact__company=company, lead__status=Lead.Status.BOOKED
    ).aggregate(total=Sum("quote_total"))["total"] or Decimal("0.00")
    orders = Lead.objects.filter(contact__company=company, status=Lead.Status.BOOKED).count()
    trips = (
        Lead.objects.filter(contact__company=company).aggregate(n=Count("reservations"))["n"] or 0
    )
    leads = list(
        Lead.objects.filter(contact__company=company).select_related("contact").order_by("-id")[:10]
    )
    return render(
        request,
        "contacts/company_detail.html",
        {
            "nav": "contacts",
            "company": company,
            "members": members,
            "leads": leads,
            "stats": {"ltv": ltv, "orders": orders, "trips": trips},
            "contacts_for_billing": [(c.pk, c.name) for c in members],
        },
    )


@login_required
@require_POST
def company_update(request: HttpRequest, pk: int) -> HttpResponse:
    """Partial-field autosave for the company profile."""
    company = get_object_or_404(Company, pk=pk)
    fields = []
    if "name" in request.POST and request.POST.get("name", "").strip():
        company.name = request.POST["name"].strip()
        fields.append("name")
    if "notes" in request.POST:
        company.notes = request.POST.get("notes", "").strip()
        fields.append("notes")
    if "billing_contact" in request.POST:
        val = (request.POST.get("billing_contact") or "").strip()
        if val:
            try:
                pk_val = int(val)
            except ValueError:
                return JsonResponse(
                    {"ok": False, "error": "Select a valid billing contact."},
                    status=400,
                )
            if not Contact.objects.filter(pk=pk_val).exists():
                return JsonResponse(
                    {"ok": False, "error": "Select a valid billing contact."},
                    status=400,
                )
            company.billing_contact_id = pk_val
        else:
            company.billing_contact_id = None
        fields.append("billing_contact")
    if fields:
        try:
            company.save(update_fields=[*fields, "updated_at"])
        except IntegrityError:
            return JsonResponse(
                {"ok": False, "error": "A company with that name already exists."},
                status=400,
            )
    return JsonResponse({"ok": True})


@login_required
@require_POST
def contact_address_update(request: HttpRequest, pk: int, slot: str) -> HttpResponse:
    """Lazy per-slot address-update endpoint. Lazily creates the slot's Address
    on first save and writes the posted fields."""
    if slot not in ("primary", "billing"):
        raise Http404("Unknown address slot.")
    contact = get_object_or_404(Contact, pk=pk)
    fk = f"{slot}_address"
    address = getattr(contact, fk)
    if address is None:
        address = Address.objects.create()
        setattr(contact, fk, address)
        contact.save(update_fields=[fk, "updated_at"])

    apply_posted_address(address, request.POST)
    return JsonResponse({"ok": True, "address_id": address.pk})
