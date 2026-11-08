import factory

from apps.accounts.factories import UserFactory

from .models import ImportRun


class ImportRunFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ImportRun

    kind = ImportRun.Kind.IMPORT
    screen = ImportRun.Screen.CONTACTS
    status = ImportRun.Status.PREVIEWED
    filename = "contacts.xlsx"
    user = factory.SubFactory(UserFactory)
