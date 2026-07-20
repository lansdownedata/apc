import pytest

from apps.contacts.models import Contact
from apps.contacts.services import backfill_phone_e164

pytestmark = pytest.mark.django_db


def test_converts_parseable_rows():
    contact = Contact.objects.create(name="Ada", phone="(617) 555-0207")
    updated = backfill_phone_e164(Contact)
    contact.refresh_from_db()
    assert contact.phone == "+16175550207"
    assert updated == 1


def test_leaves_unparseable_rows_untouched():
    """Seed junk with an invalid area code — blanking it would destroy the only detail we hold."""
    contact = Contact.objects.create(name="Jerry", phone="(734) 069-1777")
    backfill_phone_e164(Contact)
    contact.refresh_from_db()
    assert contact.phone == "(734) 069-1777"


def test_leaves_already_normalized_rows_untouched():
    contact = Contact.objects.create(name="Ada", phone="+16175550207")
    updated = backfill_phone_e164(Contact)
    contact.refresh_from_db()
    assert contact.phone == "+16175550207"
    assert updated == 0, "already-canonical rows are not rewrites"


def test_ignores_blank_phones():
    contact = Contact.objects.create(name="Nobody", phone="")
    backfill_phone_e164(Contact)
    contact.refresh_from_db()
    assert contact.phone == ""
