import factory
from django.contrib.contenttypes.models import ContentType

from apps.reservations.models import Reservation

from .models import AuditEntry


class AuditEntryFactory(factory.django.DjangoModelFactory):
    """A stored entry, for display tests. Defaults to an update of a trip from the portal."""

    class Meta:
        model = AuditEntry

    content_type = factory.LazyFunction(lambda: ContentType.objects.get_for_model(Reservation))
    object_id = factory.Sequence(lambda n: 900_000 + n)
    object_pk = factory.LazyAttribute(lambda e: str(e.object_id))
    object_repr = factory.LazyAttribute(lambda e: f"APC-{600_000 + e.object_id}")
    action = AuditEntry.Action.UPDATE
    changes = factory.LazyFunction(dict)
    source = "portal"
    trip_id = factory.LazyAttribute(lambda e: e.object_id)
