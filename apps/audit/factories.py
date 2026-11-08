import factory
from django.contrib.contenttypes.models import ContentType

from apps.reservations.models import Reservation

from .models import AuditEntry


class AuditEntryFactory(factory.django.DjangoModelFactory):
    """A stored entry, for display tests. Defaults to an update of a trip from the portal;
    `model=Charge` (any model) files it on that model instead."""

    class Meta:
        model = AuditEntry

    class Params:
        model = Reservation

    content_type = factory.LazyAttribute(lambda e: ContentType.objects.get_for_model(e.model))
    object_id = factory.Sequence(lambda n: 900_000 + n)
    object_pk = factory.LazyAttribute(lambda e: str(e.object_id))
    object_repr = factory.LazyAttribute(lambda e: f"APC-{600_000 + e.object_id}")
    action = AuditEntry.Action.UPDATE
    changes = factory.LazyFunction(dict)
    source = "portal"
    trip_id = factory.LazyAttribute(lambda e: e.object_id)
