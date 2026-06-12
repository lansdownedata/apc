import json
import secrets

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt

from . import services, webhooks

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
