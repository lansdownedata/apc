from datetime import timedelta

import factory
from django.utils import timezone

from .models import SignInCode, User


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


class SignInCodeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = SignInCode

    user = factory.SubFactory(UserFactory)
    code_hash = "0" * 64
    channel = SignInCode.Channel.SMS
    expires_at = factory.LazyFunction(lambda: timezone.now() + timedelta(minutes=10))
