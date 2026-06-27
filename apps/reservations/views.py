"""Reservation editor endpoints — JSON-draft save, duplicate, delete."""

import json

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from apps.leads.models import Lead

from .drafts import DraftError, save_reservation_from_draft


@login_required
@require_POST
def reservation_save(request):
    try:
        payload = json.loads(request.body)
    except (ValueError, TypeError):
        return HttpResponseBadRequest("invalid JSON")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("invalid payload")
    lead = get_object_or_404(Lead, pk=payload.get("lead_id"))
    instance = None
    rid = payload.get("id")
    if rid not in (None, "") and str(rid).isdigit():
        instance = lead.reservations.filter(pk=rid).first()
    try:
        save_reservation_from_draft(lead, payload, instance=instance)
    except DraftError as exc:
        return HttpResponseBadRequest(str(exc))
    return redirect("lead_detail", pk=lead.pk)
