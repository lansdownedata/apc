import pytest
from django.core.checks import Warning

from apps.integrations import checks


def test_blank_callback_secret_warns_on_deploy(settings):
    settings.DEBUG = False
    settings.GNET_CALLBACK_SECRET = ""
    [w] = checks.gnet_callback_secret_set(None)
    assert isinstance(w, Warning) and w.id == "integrations.W001"


@pytest.mark.parametrize("debug,secret", [(True, ""), (False, "s")])
def test_callback_secret_check_silent(settings, debug, secret):
    settings.DEBUG, settings.GNET_CALLBACK_SECRET = debug, secret
    assert checks.gnet_callback_secret_set(None) == []
