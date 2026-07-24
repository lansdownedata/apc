import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_lead_admin_search_by_company_does_not_crash(client, django_user_model):
    admin = django_user_model.objects.create_superuser(username="root", password="pw", email="")
    client.force_login(admin)
    # Searching by company must not raise FieldError now that company is an FK.
    resp = client.get(reverse("admin:leads_lead_changelist"), {"q": "Acme"})
    assert resp.status_code == 200
