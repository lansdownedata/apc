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


def test_farm_in_without_api_key_warns_on_deploy(settings):
    settings.GNET_FARM_IN_ENABLED, settings.GNET_API_KEY = True, ""
    [w] = checks.gnet_api_key_set_for_farm_in(None)
    assert isinstance(w, Warning) and w.id == "integrations.W002"


@pytest.mark.parametrize("enabled,key", [(False, ""), (True, "k")])
def test_farm_in_api_key_check_silent(settings, enabled, key):
    settings.GNET_FARM_IN_ENABLED, settings.GNET_API_KEY = enabled, key
    assert checks.gnet_api_key_set_for_farm_in(None) == []
