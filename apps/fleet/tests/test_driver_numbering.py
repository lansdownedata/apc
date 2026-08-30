"""Driver numbers are allocated by the system from 1000, never reused, never edited."""

from unittest.mock import patch

import pytest
from django.db import IntegrityError

from apps.fleet.models import Driver

pytestmark = pytest.mark.django_db


def test_the_first_driver_is_1000():
    assert Driver.objects.create(name="Marcus Bell").driver_number == 1000


def test_each_new_driver_gets_the_next_number():
    Driver.objects.create(name="A")
    Driver.objects.create(name="B")
    assert Driver.objects.create(name="C").driver_number == 1002


def test_a_deleted_number_is_never_reused():
    Driver.objects.create(name="A")
    gone = Driver.objects.create(name="B")  # 1001
    gone.delete()
    assert Driver.objects.create(name="C").driver_number == 1002


def test_saving_an_existing_driver_keeps_its_number():
    d = Driver.objects.create(name="A")
    d.name = "A. Bell"
    d.save()
    d.refresh_from_db()
    assert d.driver_number == 1000


def test_a_stale_allocation_is_retried_onto_the_next_free_number():
    """Simulates losing the race: the first read hands back a number another insert already
    took; the unique constraint rejects it and the allocation is retried."""
    Driver.objects.create(name="A")  # 1000
    Driver.objects.create(name="B")  # 1001
    with patch.object(Driver, "_next_number", side_effect=[1001, 1002]) as allocate:
        d = Driver.objects.create(name="C")
    assert d.driver_number == 1002
    assert allocate.call_count == 2
    assert Driver.objects.count() == 3


def test_an_allocation_that_keeps_colliding_gives_up_loudly():
    Driver.objects.create(name="A")  # 1000
    with patch.object(Driver, "_next_number", return_value=1000):
        with pytest.raises(IntegrityError):
            Driver.objects.create(name="B")
    assert Driver.objects.count() == 1


def test_number_is_not_editable():
    assert Driver._meta.get_field("driver_number").editable is False


def test_phone_is_normalized_to_e164_on_save():
    d = Driver.objects.create(name="A", phone="(571) 555-0177")
    assert d.phone == "+15715550177"


def test_status_defaults_to_active():
    assert Driver.objects.create(name="A").status == Driver.Status.ACTIVE


def test_str_shows_number_and_name():
    assert str(Driver.objects.create(name="Marcus Bell")) == "1000 · Marcus Bell"
