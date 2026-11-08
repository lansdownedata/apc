"""APC-108: a card contact's QuickBooks customer follows every edit of its name, email or phone.

On `Contact` saves, wherever they come from (the profile, the phone endpoints, booking intake,
the admin): the link is marked NOT_SYNCED for the next push. Nothing is sent from here.
"""

from __future__ import annotations

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from apps.billing.models import CustomerLink, SyncState
from apps.billing.qbo_receipts import _display_name
from apps.contacts.models import Contact

_FIELDS = ("name", "email", "phone")


def _values(name, email, phone) -> tuple[str, str, str]:
    return (name or "", email or "", phone or "")


@receiver(pre_save, sender=Contact, dispatch_uid="billing-contact-qbo-before")
def remember_customer_fields(sender, instance: Contact, raw: bool = False, **kwargs) -> None:
    """What the database holds now, for a contact with a link: one query, through the link, so
    a contact with no QuickBooks customer (most of them) costs no more than that."""
    instance._qbo_before = None
    update_fields = kwargs.get("update_fields")
    if raw or instance.pk is None or (update_fields and not set(update_fields) & set(_FIELDS)):
        return
    row = (
        CustomerLink.objects.filter(contact_id=instance.pk)
        .values_list("pk", *(f"contact__{field}" for field in _FIELDS))
        .first()
    )
    if row is not None:
        instance._qbo_before = (row[0], _values(*row[1:]))


@receiver(post_save, sender=Contact, dispatch_uid="billing-contact-qbo-requeue")
def requeue_customer(sender, instance: Contact, raw: bool = False, **kwargs) -> None:
    """A changed name, email or phone re-queues the link, clearing any old sentence (the next
    push says what is wrong now). `updated_at` moves too, so a push in flight with the old
    values never marks it SYNCED."""
    before = getattr(instance, "_qbo_before", None)
    instance._qbo_before = None
    if raw or before is None:
        return
    link_pk, values = before
    if _values(instance.name, instance.email, instance.phone) == values:
        return
    CustomerLink.objects.filter(pk=link_pk).update(
        display_name=_display_name(instance),
        qbo_sync_state=SyncState.NOT_SYNCED,
        qbo_sync_error="",
        updated_at=timezone.now(),
    )
