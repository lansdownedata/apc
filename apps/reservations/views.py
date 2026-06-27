"""Reservation editor endpoints — JSON-draft save, duplicate, delete."""

import json

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from apps.leads.models import Lead

from .drafts import DraftError, save_reservation_from_draft
from .models import Reservation, Stop


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


@login_required
@require_POST
def reservation_duplicate(request, pk):
    res = get_object_or_404(Reservation.objects.select_related("lead"), pk=pk)
    stops = list(res.stops.order_by("sequence"))
    last = res.lead.reservations.order_by("-sort_order").first()
    clone = Reservation.objects.get(pk=pk)
    clone.pk = None
    clone.service = f"{res.service or 'Reservation'} (copy)"
    clone.la_reservation_id = ""
    clone.trip_status = ""
    clone.revenue_status = Reservation.RevenueStatus.DEFERRED
    clone.recognized_at = None
    clone.recognized_amount = 0
    clone.sort_order = (last.sort_order + 1) if last else 0
    clone.save()
    Stop.objects.bulk_create(
        [
            Stop(reservation=clone, sequence=s.sequence, address=s.address, note=s.note)
            for s in stops
        ]
    )
    return redirect("lead_detail", pk=res.lead_id)


@login_required
@require_POST
def reservation_delete(request, pk):
    res = get_object_or_404(Reservation, pk=pk)
    lead_id = res.lead_id
    res.delete()
    return redirect("lead_detail", pk=lead_id)
