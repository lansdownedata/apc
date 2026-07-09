"""Fernet round-trip for stored LA customer passwords."""

import pytest
from cryptography.fernet import InvalidToken

from apps.integrations import crypto


def test_round_trip():
    token = crypto.encrypt("hunter2-secret")
    assert token != "hunter2-secret"
    assert crypto.decrypt(token) == "hunter2-secret"


def test_tampered_token_raises():
    token = crypto.encrypt("hunter2-secret")
    with pytest.raises(InvalidToken):
        crypto.decrypt(token[:-4] + "AAAA")
