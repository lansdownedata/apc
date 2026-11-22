"""The sign-in, sign-up and reset brakes: counted in the database, so every web worker
reads the same count (the default cache is one per process)."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.customers import throttle
from apps.customers.models import Throttle

pytestmark = pytest.mark.django_db

HOUR = timedelta(hours=1)


def test_a_hit_counts_up_and_over_reads_it():
    assert throttle.over("login", "10.0.0.1", 2) is False
    assert throttle.hit("login", "10.0.0.1", HOUR) == 1
    assert throttle.over("login", "10.0.0.1", 2) is False
    assert throttle.hit("login", "10.0.0.1", HOUR) == 2
    assert throttle.over("login", "10.0.0.1", 2) is True


def test_counts_are_kept_apart_by_scope_and_by_who():
    throttle.hit("login", "10.0.0.1", HOUR)
    assert throttle.over("login", "10.0.0.2", 1) is False
    assert throttle.over("reset", "10.0.0.1", 1) is False


def test_the_count_does_not_live_in_the_per_process_cache():
    throttle.hit("login", "10.0.0.1", HOUR)
    cache.clear()  # another web worker: its own, empty, cache
    assert throttle.over("login", "10.0.0.1", 1) is True


def test_the_window_runs_from_the_first_hit_and_then_starts_again():
    start = timezone.now()
    throttle.hit("reset", "a@example.com", HOUR)
    with patch("django.utils.timezone.now", return_value=start + timedelta(minutes=59)):
        assert throttle.hit("reset", "a@example.com", HOUR) == 2
    after = start + HOUR + timedelta(minutes=1)
    with patch("django.utils.timezone.now", return_value=after):
        assert throttle.over("reset", "a@example.com", 1) is False
        assert throttle.hit("reset", "a@example.com", HOUR) == 1


def test_who_is_never_stored_readably():
    throttle.hit("reset", "dana@example.com", HOUR)
    row = Throttle.objects.get()
    assert "dana" not in row.key and len(row.key) == 64


def test_a_new_counter_sweeps_out_the_ones_that_ran_out():
    start = timezone.now()
    throttle.hit("login", "10.0.0.1", HOUR)
    with patch("django.utils.timezone.now", return_value=start + timedelta(hours=2)):
        throttle.hit("login", "10.0.0.2", HOUR)
    assert Throttle.objects.count() == 1
