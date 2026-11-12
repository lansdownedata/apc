"""Field encryption for stored secrets: AES (Fernet) under FIELD_ENCRYPTION_KEY, with the old
SECRET_KEY-derived key kept for reading values written before the field key existed."""

import pytest
from cryptography.fernet import Fernet, InvalidToken
from django.core.management import call_command

from apps.integrations import crypto
from apps.integrations.checks import field_encryption_key_set

KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()


@pytest.fixture
def field_key(settings):
    settings.FIELD_ENCRYPTION_KEY = [KEY_A]
    return settings


def test_round_trip():
    token = crypto.encrypt("hunter2-secret")
    assert token != "hunter2-secret"
    assert crypto.decrypt(token) == "hunter2-secret"


def test_tampered_token_raises():
    token = crypto.encrypt("hunter2-secret")
    with pytest.raises(InvalidToken):
        crypto.decrypt(token[:-4] + "AAAA")


def test_the_field_key_encrypts_not_the_secret_key(field_key):
    token = crypto.encrypt("rt")

    assert Fernet(KEY_A).decrypt(token.encode()) == b"rt"
    field_key.FIELD_ENCRYPTION_KEY = []
    with pytest.raises(InvalidToken):
        crypto.decrypt(token)


def test_rotating_the_secret_key_no_longer_bricks_stored_values(field_key):
    token = crypto.encrypt("rt")

    field_key.SECRET_KEY = "a-brand-new-secret-key"

    assert crypto.decrypt(token) == "rt"


def test_values_written_before_the_field_key_still_read(settings):
    settings.FIELD_ENCRYPTION_KEY = []
    legacy = crypto.encrypt("rt")

    settings.FIELD_ENCRYPTION_KEY = [KEY_A]

    assert crypto.decrypt(legacy) == "rt"
    assert crypto.is_current(legacy) is False
    assert crypto.is_current(crypto.rotate(legacy)) is True


def test_a_new_key_goes_first_and_the_old_one_still_reads(field_key):
    old = crypto.encrypt("rt")

    field_key.FIELD_ENCRYPTION_KEY = [KEY_B, KEY_A]

    assert crypto.decrypt(old) == "rt"
    assert Fernet(KEY_B).decrypt(crypto.encrypt("rt").encode()) == b"rt"
    assert crypto.is_current(old) is False


def test_production_without_a_field_key_fails_the_deploy_check(settings):
    settings.DEBUG = False
    settings.FIELD_ENCRYPTION_KEY = []

    assert [e.id for e in field_encryption_key_set(None)] == ["integrations.E001"]


def test_a_malformed_field_key_fails_the_deploy_check(settings):
    settings.DEBUG = False
    settings.FIELD_ENCRYPTION_KEY = ["not-a-fernet-key"]

    assert [e.id for e in field_encryption_key_set(None)] == ["integrations.E002"]


def test_a_good_field_key_passes_the_deploy_check(field_key):
    field_key.DEBUG = False

    assert field_encryption_key_set(None) == []


# --- manage.py reencrypt_fields -------------------------------------------------------------


@pytest.mark.django_db
def test_reencrypt_moves_every_stored_secret_to_the_current_key(settings, capsys):
    from apps.integrations.factories import LACustomerFactory
    from apps.integrations.models import QboCredential

    settings.FIELD_ENCRYPTION_KEY = []
    customer = LACustomerFactory()
    cred = QboCredential.load()
    cred.set_tokens(access="at", refresh="rt", access_expires_at=None, refresh_expires_at=None)
    cred.save()

    settings.FIELD_ENCRYPTION_KEY = [KEY_A]
    call_command("reencrypt_fields")

    customer.refresh_from_db()
    cred.refresh_from_db()
    for token in (customer.password_encrypted, cred.access_token_enc, cred.refresh_token_enc):
        assert crypto.is_current(token)
    assert (cred.access_token, cred.refresh_token) == ("at", "rt")
    assert customer.password == "factory-pw"
    assert "3 re-encrypted" in capsys.readouterr().out


@pytest.mark.django_db
def test_reencrypt_twice_changes_nothing_the_second_time(field_key, capsys):
    from apps.integrations.factories import LACustomerFactory

    LACustomerFactory()
    call_command("reencrypt_fields")
    capsys.readouterr()

    call_command("reencrypt_fields")

    assert "0 re-encrypted" in capsys.readouterr().out
