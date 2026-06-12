import secrets

from django.conf import settings
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render

from . import services

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
