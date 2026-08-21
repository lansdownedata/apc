"""Reservation editor endpoints — JSON-draft save, duplicate, delete."""

from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from apps.dispatch import services as dispatch_services
from apps.leads.models import Lead
from apps.notifications.models import Notification

from .drafts import DraftError, save_reservation_from_draft
from .models import Reservation, Stop


def _alert_la_stale(reservation: Reservation) -> None:
    """The trip was already pushed to LA — flag that LA needs a manual update."""
    if not reservation.la_reservation_id:
        return
    Notification.notify(
        reservation.lead,
        Notification.Kind.LA_CHANGED,
        title="Update LimoAnywhere",
        detail=f"Trip #{reservation.pk} changed after LA sync — edit it in LimoAnywhere.",
    )


@login_required
@require_POST
def reservation_save(request) -> HttpResponse:
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
    editing_existing = instance is not None
    try:
        save_reservation_from_draft(lead, payload, instance=instance)
    except DraftError as exc:
        return HttpResponseBadRequest(str(exc))
    if editing_existing:
        _alert_la_stale(instance)
    return redirect("lead_detail", pk=lead.pk)


@login_required
@require_POST
def reservation_duplicate(request, pk) -> HttpResponse:
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
def reservation_delete(request, pk) -> HttpResponse:
    res = get_object_or_404(Reservation, pk=pk)
    lead_id = res.lead_id
    _alert_la_stale(res)
    # The trip is about to stop existing — pull any affiliate offer first, so coverage is
    # released through the one door instead of vanishing with the CASCADE.
    dispatch_services.release_trips([res], note="Trip removed")
    res.delete()
    return redirect("lead_detail", pk=lead_id)
