"""Encryption for stored secrets: QuickBooks tokens and company id, LA customer passwords.

AES through Fernet (AES-128-CBC with an HMAC-SHA256 check). The keys are
`FIELD_ENCRYPTION_KEY`, a comma-separated list of Fernet keys kept apart from every other
setting, as Intuit's security requirements ask:

* The first key encrypts; every key decrypts. To rotate, put a new key first, run
  `manage.py reencrypt_fields`, then drop the old key.
* Values written before the field key existed used a key derived from SECRET_KEY. That key
  stays last, for reading only, so nothing breaks before `reencrypt_fields` has moved them.
  With `FIELD_ENCRYPTION_KEY` blank (local development) it encrypts too.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings


def _legacy() -> Fernet:
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _keys() -> list[Fernet]:
    return [Fernet(key) for key in settings.FIELD_ENCRYPTION_KEY] + [_legacy()]


def _fernet() -> MultiFernet:
    return MultiFernet(_keys())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def is_current(token: str) -> bool:
    """Whether the encrypting key wrote this value (nothing for `reencrypt_fields` to do)."""
    try:
        _keys()[0].decrypt(token.encode())
    except InvalidToken:
        return False
    return True


def rotate(token: str) -> str:
    """The same value, encrypted under the current key."""
    return _fernet().rotate(token.encode()).decode()
