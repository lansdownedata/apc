"""ContactFactory's phone sequence must not wrap/collide across a long session."""

import phonenumbers
import pytest

from apps.contacts.factories import ContactFactory, _unique_test_phone

pytestmark = pytest.mark.django_db


def test_generated_numbers_are_valid_e164():
    for n in (0, 1, 99, 100, 101, 999, 5000, 9999):
        raw = _unique_test_phone(n)
        parsed = phonenumbers.parse(raw, "US")
        assert phonenumbers.is_valid_number(parsed)


def test_sequence_does_not_collide_past_the_old_wrap_point():
    """The old `n % 100` sequence reused a number on its 101st call. 250 factory
    calls in one session must all get distinct, always-valid numbers."""
    contacts = ContactFactory.create_batch(250)
    numbers = {c.phone for c in contacts}
    assert len(numbers) == 250
    assert "" not in numbers  # a collision would refuse-to-steal into a blank phone
