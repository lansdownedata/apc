import factory

from .models import User


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User
        django_get_or_create = ("username",)

    username = factory.Sequence(lambda n: f"agent{n}")
    email = factory.LazyAttribute(lambda u: f"{u.username}@allprocharter.com")
    role = User.Role.AGENT
