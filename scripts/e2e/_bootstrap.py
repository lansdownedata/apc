"""Shared start-up for the end-to-end helper scripts in scripts/e2e/.

Each helper does `from _bootstrap import django_ready, BASE_URL, env` before touching models.
Run helpers from the repo root with the venv active: `python scripts/e2e/<helper>.py --help`.
They read `.env` (the same file runserver uses) and target the local dev database only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")


def env(key: str, default: str = "") -> str:
    """A value from .env (or the process environment), without importing Django."""
    if key in os.environ:
        return os.environ[key]
    path = REPO / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


BASE_URL = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:8000")


def django_ready() -> None:
    """Set up Django on the dev settings; refuse any database that isn't local MySQL or SQLite."""
    import django

    django.setup()
    from django.conf import settings

    db = settings.DATABASES["default"]
    host = db.get("HOST") or "localhost"
    if host not in ("localhost", "127.0.0.1", "") and "sqlite" not in db["ENGINE"]:
        sys.exit(f"Refusing to run: database host is {host!r}, not local.")
