"""The Contacts directory's row set, shared by the list and its Excel export (APC-141)."""

from django.db.models import Exists, OuterRef, QuerySet

from apps.leads.models import Lead

from .models import Contact


def clean_scope(scope: str | None) -> str:
    """`all`, or `customers` for anything else (the directory's default)."""
    return "all" if scope == "all" else "customers"


def directory(scope: str, q: str) -> QuerySet[Contact]:
    """The contacts the directory lists for `scope` and search `q`. No annotations.

    `customers` keeps contacts with a lead: every stranger who texts the main business
    number becomes a Contact, and a contact with no lead has nothing the directory's
    columns are for. Exists, NOT .filter(leads__isnull=False) — the latter joins the leads
    table and lists a three-quote customer three times.
    """
    contacts = Contact.objects.all()
    if clean_scope(scope) != "all":
        contacts = contacts.filter(Exists(Lead.objects.filter(contact=OuterRef("pk"))))
    q = (q or "").strip()
    if q:
        # Same matching rule as the booking modal's type-ahead — see ContactManager.search.
        contacts = contacts.filter(pk__in=Contact.objects.search(q).values("pk"))
    return contacts
