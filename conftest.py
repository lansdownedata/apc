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
"""

import pytest


@pytest.fixture(autouse=True)
def _blank_aviationstack_key(settings):
    settings.AVIATIONSTACK_API_KEY = ""
