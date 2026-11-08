"""The audit frame: who did it and through what (APC-140, spec §4.3).

Our own ContextVar instead of `auditlog.context.set_actor`, which sets its value without a
token and never restores it, so a nested context silently stops stamping the outer one.
Here every frame is set with a token and reset on exit, so nesting restores the outer frame.

`actor`, `source` and `actor_email` may be callables. They are evaluated the first time an
entry is written in the frame, at most once, so a request that changes nothing never loads
its user.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apps.accounts.models import User


@dataclass
class Frame:
    actor: Any
    source: Any
    cid: str
    actor_email: Any = ""
    resolved: tuple[User | None, str, str] | None = field(default=None, repr=False)


_frame: ContextVar[Frame | None] = ContextVar("audit_frame", default=None)


def new_cid() -> str:
    return uuid.uuid4().hex


@contextmanager
def audit_context(
    *, actor: Any = None, source: Any, cid: str | None = None, actor_email: Any = ""
) -> Iterator[Frame]:
    frame = Frame(actor, source, cid or new_cid(), actor_email)
    token = _frame.set(frame)
    try:
        yield frame
    finally:
        _frame.reset(token)


def current_cid() -> str | None:
    """auditlog's `AUDITLOG_CID_GETTER`: the frame's id, never a request header."""
    frame = _frame.get()
    return frame.cid if frame else None


def fallback_source() -> str:
    """Where a write with no frame came from: the management command, else the system."""
    if Path(sys.argv[0]).name == "manage.py" and len(sys.argv) > 1:
        return f"command: {sys.argv[1]}"
    return "system"


def _value(value: Any) -> Any:
    return value() if callable(value) else value


def resolve() -> tuple[User | None, str, str]:
    """`(actor, actor_email, source)` for the entry being written now."""
    frame = _frame.get()
    if frame is None:
        return None, "", fallback_source()
    if frame.resolved is None:
        actor = _value(frame.actor)
        actor_email = _value(frame.actor_email) or (getattr(actor, "email", "") if actor else "")
        frame.resolved = (actor, actor_email or "", _value(frame.source))
    return frame.resolved
