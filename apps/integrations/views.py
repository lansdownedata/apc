import hashlib
import hmac
import json
import logging
import secrets

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    JsonResponse,
)
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.dispatch import services as dispatch_services
from apps.dispatch.gnet_callback import handle_callback
from apps.messaging import touchpoints
from apps.notifications.models import Notification
from apps.reservations.models import EARNED_TERMINAL_STATUSES, Reservation, TripStatusEvent

from . import la_sync, services, webhooks
from .geocoding import merged_autocomplete
from .models import LACustomer, LAEvent

logger = logging.getLogger(__name__)

STATE_SESSION_KEY = "podium_oauth_state"

# Cancelled-family statuses, per apps/reservations/models.py TRIP_PHASE_BY_STATUS. A trip
# in one of these will never be "done" but is finished from a review-invite standpoint.
_CANCELLED_STATUSES = (
    Reservation.TripStatus.CANCELLED,
    Reservation.TripStatus.CANCELLED_BY_AFFILIATE,
    Reservation.TripStatus.LATE_CANCEL,
    Reservation.TripStatus.COVID_CANCELLATION,
)
_TERMINAL_STATUSES = EARNED_TERMINAL_STATUSES + _CANCELLED_STATUSES


def _digests_equal(expected: str, received: str) -> bool:
    """Constant-time compare of two header-sourced strings, as BYTES.

    `hmac.compare_digest` raises `TypeError: comparing strings with non-ASCII
    characters is not supported` when either `str` argument has a codepoint above
    U+007F. Both call sites here feed it a raw request header, so a single non-ASCII
    byte from an unauthenticated caller would turn a 403 into a 500 on a public
    endpoint. Encoding first is defined for every input and keeps the comparison
    constant-time.
    """
    return hmac.compare_digest(expected.encode(), received.encode())


def _podium_signature_ok(request: HttpRequest) -> bool:
    """HMAC-SHA256 over '{podium-timestamp}.{raw_body}' with PODIUM_WEBHOOK_SECRET.

    Blank secret => accept (dev); set secret => fail closed.
    """
    secret = settings.PODIUM_WEBHOOK_SECRET
    if not secret:
        return True
    timestamp = request.headers.get("Podium-Timestamp", "")
    signature = request.headers.get("Podium-Signature", "")
    if not timestamp or not signature:
        return False
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + request.body, hashlib.sha256
    ).hexdigest()
    return _digests_equal(expected, signature)


def _gnet_signature_ok(request: HttpRequest) -> bool:
    """Verify both credentials the gateway sends (contract v2 §5.8):
    `Authorization: Bearer <GNET_CALLBACK_SECRET>` and an HMAC-SHA256 of the RAW
    body, also keyed with GNET_CALLBACK_SECRET, in `X-Lansdowne-Signature`.

    Blank secret => accept (dev); set => fail closed on either check. Mirrors
    _podium_signature_ok. The HMAC is the cryptographically stronger check — the
    Bearer comparison is defence in depth, not a substitute for it. The signature
    covers raw bytes: re-serialising the JSON changes key order and whitespace and
    will never match.
    """
    secret = settings.GNET_CALLBACK_SECRET
    if not secret:
        return True
    auth_header = request.headers.get("Authorization", "")
    if not _digests_equal(f"Bearer {secret}", auth_header):
        return False
    header = request.headers.get("X-Lansdowne-Signature", "")
    if not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
    return _digests_equal(expected, header.removeprefix("sha256="))


def _calendly_signature_ok(request: HttpRequest) -> bool:
    """HMAC-SHA256 over '{t}.{raw_body}' with CALENDLY_WEBHOOK_SIGNING_KEY.

    Calendly sends `Calendly-Webhook-Signature: t=<unix seconds>,v1=<hex digest>`
    (confirmed against its webhook-signatures docs, 2026-08-31). Blank key => accept
    (dev); set => fail closed, as for Podium and GNet. The HMAC covers the RAW bytes:
    re-serialising the JSON changes key order and whitespace and will never match.

    No timestamp-tolerance check, deliberately. Calendly suggests one, but a valid
    signature only ever replays a genuine past payload and the unique idempotency key
    already makes that a no-op — while retries use exponential backoff across 24
    hours, so a strict window would reject every retry after an outage and get the
    subscription disabled. Availability is the bigger risk here than replay.
    """
    key = settings.CALENDLY_WEBHOOK_SIGNING_KEY
    if not key:
        return True
    header = request.headers.get("Calendly-Webhook-Signature", "")
    parts = dict(piece.split("=", 1) for piece in header.split(",") if "=" in piece)
    timestamp, signature = parts.get("t", ""), parts.get("v1", "")
    if not timestamp or not signature:
        logger.warning("Calendly webhook: unusable signature header %r", header[:200])
        return False
    expected = hmac.new(
        key.encode(), f"{timestamp}.".encode() + request.body, hashlib.sha256
    ).hexdigest()
    if _digests_equal(expected, signature):
        return True
    # The header format is the one thing untestable until real traffic arrives; a bare
    # 403 with no record of what was sent makes the first live delivery useless as
    # evidence. The key itself is never logged.
    logger.warning("Calendly webhook: signature mismatch for header %r", header[:200])
    return False


@csrf_exempt
def calendly_webhook(request):
    """Receive Calendly invitee webhooks (invitee.created / invitee.canceled).

    Answers 200 for anything structurally unusable rather than 4xx: Calendly retries
    a non-2xx for 24 hours and then DISABLES the subscription, so a payload that can
    never be processed must not be refused. Only a bad signature and unparseable JSON
    are turned away.
    """
    if request.method != "POST":
        return HttpResponseBadRequest("POST only.")
    if not _calendly_signature_ok(request):
        return HttpResponse(status=403)
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON.")
    if not isinstance(payload, dict):
        logger.warning(
            "Calendly webhook: valid JSON but not an object (got %s) — ignored.",
            type(payload).__name__,
        )
        return HttpResponse(status=200)
    webhooks.process_calendly_webhook(payload)
    return HttpResponse(status=200)


def podium_authorize(request):
    """Kick off the Podium OAuth flow — redirect the user to grant access."""
    if not settings.PODIUM_CLIENT_ID:
        return HttpResponseBadRequest("Podium client is not configured.")
    state = secrets.token_urlsafe(24)
    request.session[STATE_SESSION_KEY] = state
    return redirect(services.build_authorize_url(state))


def podium_callback(request):
    """Handle Podium's redirect: validate state, exchange the code for tokens."""
    if error := request.GET.get("error"):
        return render(request, "integrations/podium_callback.html", {"error": error})

    state = request.GET.get("state")
    if not state or state != request.session.pop(STATE_SESSION_KEY, None):
        return HttpResponseBadRequest("Invalid OAuth state.")

    code = request.GET.get("code")
    if not code:
        return HttpResponseBadRequest("Missing authorization code.")

    credential = services.exchange_code(code)
    return render(request, "integrations/podium_callback.html", {"credential": credential})


@csrf_exempt
def podium_webhook(request):
    """Receive Podium message webhooks (message.received / sent / failed)."""
    if request.method != "POST":
        return HttpResponseBadRequest("POST only.")
    if not _podium_signature_ok(request):
        return HttpResponse(status=403)
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON.")
    webhooks.process_podium_webhook(payload)
    return HttpResponse(status=200)


@csrf_exempt
@require_POST
def la_webhook(request, token: str):
    """Inbound LimoAnywhere reservation events (per-customer signed-token URL)."""
    try:
        la_customer_pk = signing.loads(token, salt=la_sync.WEBHOOK_SALT)
    except signing.BadSignature:
        raise Http404 from None
    la_customer = LACustomer.objects.filter(pk=la_customer_pk).first()
    if la_customer is None:
        raise Http404

    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON.")

    event_name = str(data.get("reservation_event") or "")
    # Only look up reservation if id is truthy; falsy ids should not match un-pushed reservations.
    id_value = data.get("id")
    if id_value:
        reservation = Reservation.objects.filter(
            la_reservation_id=str(id_value), lead__contact=la_customer.contact
        ).first()
    else:
        reservation = None
    LAEvent.objects.create(
        la_customer=la_customer, reservation=reservation, event=event_name, payload=data
    )
    if reservation is None:
        return JsonResponse({"status": "ignored"})

    new_status = la_sync.LA_EVENT_TO_TRIP_STATUS.get(event_name)
    if new_status and new_status != reservation.trip_status:
        reservation.trip_status = new_status
        reservation.save(update_fields=["trip_status", "updated_at"])
        TripStatusEvent.objects.create(
            reservation=reservation,
            status=new_status,
            source=TripStatusEvent.Source.LIMOANYWHERE,
        )
        if new_status in EARNED_TERMINAL_STATUSES:
            lead = reservation.lead
            if all(r.trip_status in _TERMINAL_STATUSES for r in lead.reservations.all()):
                touchpoints.schedule_review_request(lead)
    if event_name == "reservation.cancelled":
        # LimoAnywhere is the system of record, so a cancellation there ends the trip —
        # and an affiliate holding it has to be let go. Without this the assignment stays
        # active with no screen that lists it, which on the GNet channel means a real
        # affiliate keeps a live booking for a trip that no longer exists. Best-effort by
        # construction: `release_trips` isolates and logs each assignment's failure rather
        # than raising, so a gateway problem can never 500 this webhook into an LA retry
        # of a cancellation already applied here.
        dispatch_services.release_trips([reservation], note="Cancelled in LimoAnywhere")
    if event_name in {"reservation.cancelled", "reservation.updated"}:
        Notification.notify(
            reservation.lead,
            Notification.Kind.LA_CHANGED,
            title="Changed in LimoAnywhere",
            detail=f"Trip #{reservation.pk}: {event_name.removeprefix('reservation.')}",
        )
    return JsonResponse({"status": "ok"})


@csrf_exempt
def gnet_callback(request):
    """Receive GNet farm-out status callbacks (contract v2 §5.8) and resolve the
    assignment they concern.

    Unlike farm-in (§1), the response body is ignored by the gateway — only the
    HTTP status matters — so this answers 2xx as fast as possible and never surfaces
    processing detail in the body. The gateway holds a 15s budget and retries a
    non-2xx (including a timeout) up to 3 times with backoff plus a background
    sweeper, so every internal failure mode `handle_callback` can hit — an
    unrecognised status, an already-resolved assignment, an uncorrelated
    transactionId — is swallowed there and still answered 2xx here; only a bad
    signature returns non-2xx.

    A syntactically valid JSON body that isn't an object (`[1,2,3]`, `"str"`, a
    bare number/bool/`null`) parses fine — `json.loads` doesn't raise for any of
    those — but has no `.get`, so it's rejected here before reaching
    `handle_callback` rather than crashing there. That's answered 200 too: the
    gateway ignores the body and retries any non-2xx, so a 4xx would only buy a
    guaranteed-to-fail retry storm for a request that can never succeed. Logged so
    a genuinely malformed sender is still visible.
    """
    if request.method != "POST":
        return HttpResponseBadRequest("POST only.")
    if not _gnet_signature_ok(request):
        return HttpResponse(status=403)
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON.")
    if not isinstance(payload, dict):
        logger.warning(
            "GNet callback: valid JSON but not an object (got %s) — ignored.",
            type(payload).__name__,
        )
        return HttpResponse(status=200)
    handle_callback(payload)
    return HttpResponse(status=200)


@login_required
@require_GET
def geocode_autocomplete(request):
    """Server proxy for LocationIQ autocomplete, with airport matches prepended.

    Airports come from the local table, so results are returned even with no API key —
    `degraded` tells the client LocationIQ itself is unavailable.
    """
    results = merged_autocomplete(
        request.GET.get("q", ""),
        lat=request.GET.get("lat"),
        lon=request.GET.get("lon"),
    )
    return JsonResponse({"results": results, "degraded": not settings.LOCATIONIQ_API_KEY})
