"""Podium inbox — conversation list, thread view, and outbound send.

The Podium API call itself lives in `apps.integrations.podium`; these views stay thin
(build the queryset / validate / persist the resulting Message row).
"""

from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Max, Q, QuerySet
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.integrations import podium
from apps.integrations.podium import PodiumAPIError, PodiumNotConnected
from apps.leads.models import Lead

from .models import Message

CHANNEL_TYPE = {"sms": "phone", "email": "email"}
CHANNEL_MODEL = {"sms": Message.Channel.SMS, "email": Message.Channel.EMAIL}


def _conversations(q: str = "") -> QuerySet[Lead]:
    """Leads with at least one message, annotated for the inbox list."""
    qs = (
        Lead.objects.filter(messages__isnull=False)
        .select_related("contact")
        .annotate(
            last_message_at=Max("messages__created_at"),
            unread_count=Count(
                "messages",
                filter=Q(
                    messages__direction=Message.Direction.IN,
                    messages__read_at__isnull=True,
                ),
                distinct=True,
            ),
        )
        .distinct()
        .order_by("-last_message_at")
    )
    if q:
        qs = qs.filter(
            Q(contact__name__icontains=q)
            | Q(contact__company__icontains=q)
            | Q(contact__phone__icontains=q)
        )
    return qs


@login_required
def inbox(request):
    """Conversation list + (optionally) the selected thread, marking it read."""
    q = request.GET.get("q", "").strip()

    selected = None
    thread_messages: list[Message] = []
    la_confirmation = ""
    lead_pk = request.GET.get("lead")
    if lead_pk:
        # Validate that lead_pk is numeric to avoid ValueError → 500.
        if not lead_pk.isdigit():
            raise Http404
        selected = get_object_or_404(
            Lead.objects.filter(messages__isnull=False).select_related("contact").distinct(),
            pk=lead_pk,
        )
        # Mark read before building list so response reflects cleared unread count.
        Message.objects.filter(
            lead=selected, direction=Message.Direction.IN, read_at__isnull=True
        ).update(read_at=timezone.now())
        thread_messages = list(selected.messages.all())
        for reservation in selected.reservations.all():
            if reservation.la_confirmation:
                la_confirmation = reservation.la_confirmation
                break

    conversations = list(_conversations(q))

    return render(
        request,
        "messaging/inbox.html",
        {
            "nav": "inbox",
            "page_title": "Inbox",
            "conversations": conversations,
            "selected": selected,
            "thread_messages": thread_messages,
            "la_confirmation": la_confirmation,
            "q": q,
        },
    )


@login_required
@require_POST
def inbox_send(request, pk: int) -> JsonResponse:
    """Send an outbound SMS/email through Podium and record the Message row."""
    lead = get_object_or_404(Lead.objects.select_related("contact"), pk=pk)
    try:
        payload = json.loads(request.body or b"{}")
    except (ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"ok": False, "error": "Invalid payload."}, status=400)

    channel = (payload.get("channel") or "").strip()
    if not channel or channel not in CHANNEL_TYPE:
        return JsonResponse({"ok": False, "error": "Channel must be 'sms' or 'email'."}, status=400)

    body = (payload.get("body") or "").strip()
    if not body:
        return JsonResponse({"ok": False, "error": "Message body cannot be blank."}, status=400)

    identifier = lead.contact.phone if channel == "sms" else lead.contact.email
    if not identifier:
        label = "phone number" if channel == "sms" else "email address"
        return JsonResponse(
            {"ok": False, "error": f"This contact has no {label} on file."}, status=400
        )

    try:
        response = podium.send_message(
            identifier=identifier, body=body, channel_type=CHANNEL_TYPE[channel]
        )
    except (PodiumAPIError, PodiumNotConnected) as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=502)

    uid = ""
    if isinstance(response, dict):
        uid = response.get("uid") or (response.get("data") or {}).get("uid") or ""

    message = Message.objects.create(
        lead=lead,
        direction=Message.Direction.OUT,
        channel=CHANNEL_MODEL[channel],
        body=body,
        podium_message_uid=uid,
        sent_at=timezone.now(),
        delivery_status=Message.DeliveryStatus.SENT,
    )
    return JsonResponse(
        {
            "ok": True,
            "message": {
                "id": message.pk,
                "direction": message.direction,
                "channel": message.channel,
                "body": message.body,
                "sent_at": message.sent_at.isoformat(),
            },
        }
    )
