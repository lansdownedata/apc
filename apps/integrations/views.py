import json
import secrets

from django.conf import settings
from django.core import signing
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.notifications.models import Notification
from apps.reservations.models import Reservation, TripStatusEvent

from . import la_sync, services, webhooks
from .models import LACustomer, LAEvent

STATE_SESSION_KEY = "podium_oauth_state"


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
    # TODO: verify the Podium signature against PODIUM_WEBHOOK_SECRET once the
    # signing scheme is confirmed from the Developer Portal.
    if request.method != "POST":
        return HttpResponseBadRequest("POST only.")
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
        reservation = Reservation.objects.filter(la_reservation_id=str(id_value)).first()
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
    if event_name in {"reservation.cancelled", "reservation.updated"}:
        Notification.notify(
            reservation.lead,
            Notification.Kind.LA_CHANGED,
            title="Changed in LimoAnywhere",
            detail=f"Trip #{reservation.pk}: {event_name.removeprefix('reservation.')}",
        )
    return JsonResponse({"status": "ok"})
