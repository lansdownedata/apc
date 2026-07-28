import factory
from django.utils import timezone

from .models import User


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User
        django_get_or_create = ("username",)

    username = factory.Sequence(lambda n: f"agent{n}")
    email = factory.LazyAttribute(lambda u: f"{u.username}@allprocharter.com")
    role = User.Role.AGENT

    class Params:
        pending = factory.Trait(
            invited_at=factory.LazyFunction(timezone.now),
            invite_accepted_at=None,
        )
        deactivated = factory.Trait(is_active=False)
