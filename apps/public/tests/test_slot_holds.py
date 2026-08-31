"""SlotHold — advisory locks on a Calendly slot while a visitor fills the form.

A hold stops two of OUR visitors racing each other into the same slot. It is never
proof a slot is free: someone can book the same time on calendly.com, or the host can
put a meeting in their own calendar, and neither touches this table. Calendly's
`already_filled` is the only authority (see the plan's decision 6).
"""

from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from apps.public.models import SlotHold

pytestmark = pytest.mark.django_db

SLOT = datetime(2026, 9, 3, 21, 15, tzinfo=timezone.get_fixed_timezone(0))
OTHER_SLOT = datetime(2026, 9, 3, 21, 45, tzinfo=timezone.get_fixed_timezone(0))


def test_a_claim_returns_the_hold():
    hold = SlotHold.objects.claim(SLOT, "session-a")
    assert hold is not None
    assert hold.session_key == "session-a"
    assert hold.start_time == SLOT
    assert hold.expires_at > timezone.now()


def test_a_second_visitor_cannot_take_a_live_hold():
    SlotHold.objects.claim(SLOT, "session-a")
    assert SlotHold.objects.claim(SLOT, "session-b") is None


def test_a_different_slot_is_unaffected():
    SlotHold.objects.claim(SLOT, "session-a")
    assert SlotHold.objects.claim(OTHER_SLOT, "session-b") is not None


def test_re_claiming_your_own_hold_is_a_no_op_not_a_conflict():
    """The double-click case. A visitor who submits twice must not lock themselves out
    of the slot they are in the middle of booking."""
    first = SlotHold.objects.claim(SLOT, "session-a")
    second = SlotHold.objects.claim(SLOT, "session-a")
    assert second is not None
    assert second.pk == first.pk


def test_an_expired_hold_does_not_block_a_new_claim():
    hold = SlotHold.objects.claim(SLOT, "session-a")
    hold.expires_at = timezone.now() - timedelta(seconds=1)
    hold.save(update_fields=["expires_at"])
    taken = SlotHold.objects.claim(SLOT, "session-b")
    assert taken is not None
    assert taken.session_key == "session-b"


def test_active_excludes_expired_rows():
    SlotHold.objects.claim(SLOT, "session-a")
    stale = SlotHold.objects.claim(OTHER_SLOT, "session-b")
    stale.expires_at = timezone.now() - timedelta(minutes=1)
    stale.save(update_fields=["expires_at"])
    assert [h.start_time for h in SlotHold.objects.active()] == [SLOT]


def test_release_frees_the_slot_for_someone_else():
    """Called after a booking succeeds — the slot is Calendly's problem from then on,
    and leaving the hold up would grey out a slot that is genuinely gone anyway."""
    SlotHold.objects.claim(SLOT, "session-a")
    SlotHold.objects.release(SLOT, "session-a")
    assert SlotHold.objects.claim(SLOT, "session-b") is not None


def test_release_by_a_stranger_leaves_the_hold_alone():
    """Otherwise any visitor could free another's slot by posting its start time."""
    SlotHold.objects.claim(SLOT, "session-a")
    SlotHold.objects.release(SLOT, "session-b")
    assert SlotHold.objects.claim(SLOT, "session-c") is None


def test_held_start_times_is_a_set_of_live_holds_only():
    """What the slots view greys out. Expired rows must not linger in it."""
    SlotHold.objects.claim(SLOT, "session-a")
    stale = SlotHold.objects.claim(OTHER_SLOT, "session-b")
    stale.expires_at = timezone.now() - timedelta(minutes=1)
    stale.save(update_fields=["expires_at"])
    assert SlotHold.objects.held_start_times() == {SLOT}


def test_one_row_per_slot_however_often_it_changes_hands(settings):
    """The uniqueness is total, not conditional: prod Postgres has partial indexes and
    MySQL (local + test) does not, so a conditional UniqueConstraint would be enforced
    in prod and silently absent here. One row per slot, recycled, sidesteps that."""
    settings.CALENDLY_HOLD_MINUTES = 10
    for session in ("a", "b", "c"):
        hold = SlotHold.objects.claim(SLOT, f"session-{session}")
        if hold:
            hold.expires_at = timezone.now() - timedelta(seconds=1)
            hold.save(update_fields=["expires_at"])
    assert SlotHold.objects.filter(start_time=SLOT).count() == 1


def test_hold_length_comes_from_settings(settings):
    settings.CALENDLY_HOLD_MINUTES = 3
    hold = SlotHold.objects.claim(SLOT, "session-a")
    assert timedelta(minutes=2, seconds=50) < hold.expires_at - timezone.now()
    assert hold.expires_at - timezone.now() <= timedelta(minutes=3)
