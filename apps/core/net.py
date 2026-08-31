"""Who is calling — the one answer every public per-IP limit depends on."""

from django.conf import settings

UNKNOWN_CLIENT = "unknown"


def client_ip(request) -> str:
    """The visitor's IP, read correctly for however many proxies sit in front of us.

    `REMOTE_ADDR` on its own is wrong behind a reverse proxy: on Heroku it is the
    router, so every visitor on the site looks like the same caller and they all share
    one throttle bucket. Blindly trusting `X-Forwarded-For` is worse — it is a
    client-supplied header, so anyone could hand themselves a fresh bucket per request
    and the limit would stop meaning anything.

    The safe read is positional. A proxy *appends* the peer it accepted the connection
    from, so with N trusted proxies in front, the Nth entry from the right is the last
    one a proxy wrote and the first one no client could have forged. Everything to its
    left is hearsay. `TRUSTED_PROXY_COUNT = 0` (dev, and any direct deployment) skips
    the header entirely.

    A header shorter than the proxy count means it isn't the shape we expect — someone
    stripped it, or the deployment changed — so fall back to the peer rather than
    letting the caller choose their own bucket.
    """
    proxies = getattr(settings, "TRUSTED_PROXY_COUNT", 0)
    peer = request.META.get("REMOTE_ADDR") or UNKNOWN_CLIENT
    if proxies < 1:
        return peer
    forwarded = [
        part.strip()
        for part in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")
        if part.strip()
    ]
    if len(forwarded) < proxies:
        return peer
    return forwarded[-proxies]
