import pytest

from apps.addresses.models import Address

pytestmark = pytest.mark.django_db


def test_is_blank_true_when_no_user_fields():
    assert Address.objects.create().is_blank is True


def test_is_blank_false_when_line1_set():
    assert Address.objects.create(line1="14 Beacon St").is_blank is False


def test_str_prefers_landmark_then_line1_then_display_name():
    assert str(Address(landmark_name="Logan Airport", line1="1 Harborside")) == "Logan Airport"
    assert str(Address(line1="14 Beacon St")) == "14 Beacon St"
    assert str(Address(display_name="somewhere, MA")) == "somewhere, MA"
    assert str(Address()) == "Address (empty)"
