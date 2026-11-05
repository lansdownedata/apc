"""Root pytest conftest — safety nets that apply to the whole suite.

Autouse: nothing in this suite should ever be able to reach the real aviationstack API.
`config.settings.dev` (the settings module pytest runs under) reads `AVIATIONSTACK_API_KEY`
straight from `.env`, which holds Moe's real, rate-limited key. Every test that exercises
`apps.integrations.aviationstack` today mocks `requests` — but `_request()` refuses to build
a request at all when the key is blank (`AviationstackError("not_configured", ...)` raises
before `requests.get` is ever called), so blanking the key here is a *complete* guard against
an unmocked test spending the real key's quota against production, not just a partial one.
Tests that specifically need a key set one explicitly via the `settings` fixture, after this
fixture has already run (see `apps/integrations/tests/test_aviationstack.py`'s own autouse
`key` fixture, and the individual `settings.AVIATIONSTACK_API_KEY = "k"` lines elsewhere) —
a same-scope autouse fixture defined in a test module runs after one from a conftest.py
higher up the tree, so those still win.

Autouse: the same guard for LocationIQ. `config.settings.dev` reads the real key from
`.env` too, and the venue typeahead merges a LocationIQ lookup into any query the curated
directory answers thinly — so an unmocked test spends real, billed lookups. `geocoding.
autocomplete()` returns [] on a blank key before it builds a request, so this is a complete
guard. Tests that want LocationIQ rows monkeypatch the client (which bypasses the key check
entirely) or set a key via the `settings` fixture.

Autouse: the same guard for Stripe (APC-121). `.env` holds a real test-mode secret key, so
an unmocked call created real objects in the Stripe test account, and a fresh test database
reused contact pks that earlier runs had already sent under the same idempotency key, which
Stripe refused. With the key blank, an unmocked call fails with an `AuthenticationError`
before it leaves the machine. Tests mock the Stripe client they use.

Autouse: an empty cache per test. The default cache is LocMemCache, which lives for the
whole pytest process, so the public site's per-IP throttle counters leak from one test
into the next — a test that POSTs a few bookings silently spends another file's budget,
and the victim fails only when the suite runs in a particular order. Clearing between
tests makes those counters mean what each test thinks they mean.

Autouse: the go-live switch is ON for the suite (APC-90). `OUTBOUND_ENABLED` defaults to
False everywhere else, and with it off every customer and affiliate send is refused — which
is right for a portal that is not live, and wrong for a suite whose job is to prove what
those sends say. The tests about the switch itself turn it back off via the `settings`
fixture (see `apps/core/tests/test_outbound.py`).

Autouse: quiet hours are off for the suite (APC-114). Their window defaults to 9 PM to 8 AM
in the trip's zone and the clock is real, so without this every test that sends a text
would pass by day and fail by night. Tests about quiet hours turn them back on with
`@pytest.mark.quiet_hours` (see `apps/messaging/tests/test_quiet_hours.py`), and so does the
golden recording (`apps/messaging/tests/test_golden.py`): its scenarios run on a frozen clock
in the daytime, so a text held there means the hold logic is wrong.
"""

import pytest
from django.core.cache import cache

# Modules that run with real quiet hours, as if each test carried the marker.
_REAL_QUIET_HOURS = ("apps/messaging/tests/test_golden.py",)


def pytest_configure(config):
    config.addinivalue_line("markers", "quiet_hours: run with real quiet hours (APC-114)")


def pytest_collection_modifyitems(items):
    for item in items:
        if item.nodeid.startswith(_REAL_QUIET_HOURS):
            item.add_marker(pytest.mark.quiet_hours)


@pytest.fixture(autouse=True)
def _blank_aviationstack_key(settings):
    settings.AVIATIONSTACK_API_KEY = ""


@pytest.fixture(autouse=True)
def _blank_locationiq_key(settings):
    settings.LOCATIONIQ_API_KEY = ""


@pytest.fixture(autouse=True)
def _blank_stripe_key(settings):
    settings.STRIPE_SECRET_KEY = ""


@pytest.fixture(autouse=True)
def _outbound_on(settings):
    settings.OUTBOUND_ENABLED = True


@pytest.fixture(autouse=True)
def _empty_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _quiet_hours_off(request, monkeypatch):
    if request.node.get_closest_marker("quiet_hours") is None:
        monkeypatch.setattr("apps.messaging.quiet.hold_until", lambda *_, **__: None)
