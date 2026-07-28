"""Podium inbox — conversation list, thread view, and outbound send.

The Podium API call itself lives in `apps.integrations.podium`; these views stay thin
(build the queryset / validate / persist the resulting Message row).
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP

from django.contrib.auth.decorators import login_required
from django.db.models import Avg, Count, DecimalField, Q, QuerySet
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.choices import Channel
from apps.integrations import podium
from apps.integrations.podium import PodiumAPIError, PodiumNotConnected
from apps.leads.models import Lead

from . import services, touchpoints
from .models import Conversation, Message, Review
from .touchpoints import PODIUM_CHANNEL

# Podium calls the SMS channel "phone" — shared with apps.messaging.touchpoints so the
# mapping isn't duplicated (a divergent copy here is what caused SMS touch-points to be
# silently rejected).
CHANNEL_TYPE = PODIUM_CHANNEL
CHANNEL_MODEL = {"sms": Message.Channel.SMS, "email": Message.Channel.EMAIL}

FILTERS = ("open", "archived", "all")
FILTER_TABS = [("open", "Open"), ("archived", "Archived"), ("all", "All")]


def _conversations(q: str = "", conversation_filter: str = "open") -> QuerySet[Conversation]:
    """Conversations with at least one message, annotated for the inbox list."""
    qs = (
        Conversation.objects.filter(messages__isnull=False)
        .select_related("contact", "contact__company")
        .annotate(
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
        .order_by("-last_message_at", "-id")
    )
    if conversation_filter == "open":
        qs = qs.filter(status=Conversation.Status.OPEN)
    elif conversation_filter == "archived":
        qs = qs.filter(status=Conversation.Status.ARCHIVED)
    if q:
        qs = qs.filter(
            Q(contact__name__icontains=q)
            | Q(contact__company__name__icontains=q)
            | Q(contact__phone__icontains=q)
        )
    return qs


@login_required
def inbox(request):
    """Conversation list + (optionally) the selected thread, marking it read."""
    q = request.GET.get("q", "").strip()
    conversation_filter = request.GET.get("filter", "open")
    if conversation_filter not in FILTERS:
        conversation_filter = "open"

    selected = None
    thread_messages: list[Message] = []
    leads: list[Lead] = []
    conversation_pk = request.GET.get("conversation")
    if conversation_pk:
        # Validate that conversation_pk is numeric to avoid ValueError → 500.
        if not conversation_pk.isdigit():
            raise Http404
        selected = get_object_or_404(
            Conversation.objects.filter(messages__isnull=False)
            .select_related("contact", "contact__company")
            .distinct(),
            pk=conversation_pk,
        )
        # Mark read before building list so response reflects cleared unread count.
        Message.objects.filter(
            conversation=selected, direction=Message.Direction.IN, read_at__isnull=True
        ).update(read_at=timezone.now())
        thread_messages = list(selected.messages.all())
        # One conversation can spawn several quotes — the rail shows them all, so an
        # agent sees "2 quotes already" instead of creating a third by accident.
        leads = list(
            selected.contact.leads.select_related("payment")
            .prefetch_related("reservations")
            .order_by("-id")
        )

    return render(
        request,
        "messaging/inbox.html",
        {
            "nav": "inbox",
            "page_title": "Inbox",
            "conversations": list(_conversations(q, conversation_filter)),
            "selected": selected,
            "thread_messages": thread_messages,
            "leads": leads,
            "conversation_filter": conversation_filter,
            "filter_tabs": FILTER_TABS,
            "q": q,
        },
    )


@login_required
@require_POST
def inbox_send(request, pk: int) -> JsonResponse:
    """Send an outbound SMS/email through Podium and record the Message row."""
    conversation = get_object_or_404(Conversation.objects.select_related("contact"), pk=pk)
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

    identifier = conversation.contact.phone if channel == "sms" else conversation.contact.email
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

    message = services.record_outbound(
        conversation,
        channel=CHANNEL_MODEL[channel],
        body=body,
        podium_message_uid=uid,
        sender_name=request.user.get_full_name() or request.user.username,
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


@login_required
@require_POST
def conversation_archive(request: HttpRequest, pk: int) -> HttpResponse:
    """Mark a conversation as not-a-lead.

    Permanent until an agent unarchives it — later inbound messages are still recorded
    (see apps.messaging.services) but do not reopen the thread.
    """
    conversation = get_object_or_404(Conversation, pk=pk)
    conversation.status = Conversation.Status.ARCHIVED
    conversation.archived_at = timezone.now()
    conversation.archived_by = request.user
    conversation.save(update_fields=["status", "archived_at", "archived_by", "updated_at"])
    return redirect("inbox")


@login_required
@require_POST
def conversation_unarchive(request: HttpRequest, pk: int) -> HttpResponse:
    conversation = get_object_or_404(Conversation, pk=pk)
    conversation.status = Conversation.Status.OPEN
    conversation.archived_at = None
    conversation.archived_by = None
    conversation.save(update_fields=["status", "archived_at", "archived_by", "updated_at"])
    return redirect(f"{reverse('inbox')}?conversation={conversation.pk}")


@login_required
@require_POST
def conversation_create_lead(request: HttpRequest, pk: int) -> HttpResponse:
    """Qualify a conversation into a lead.

    Intentionally repeatable: one conversation can spawn several quotes, and each click
    produces a distinct lead. Mirrors `apps.leads.views.lead_create` minus the form —
    the contact already exists, and trip details belong in the quote workspace.
    """
    conversation = get_object_or_404(Conversation.objects.select_related("contact"), pk=pk)
    lead = Lead.objects.create(
        contact=conversation.contact,
        channel=Channel.PHONE,
        assigned_agent=request.user,
        status=Lead.Status.NEW,
    )
    touchpoints.schedule_lead_created(lead)
    return redirect("lead_detail", pk=lead.pk)


@login_required
def review_list(request: HttpRequest) -> HttpResponse:
    """Review invites — delivery status + incoming ratings, newest first.

    One aggregate query: average + response count over rated reviews, plus a count of
    invites still outstanding (sent/pending, no rating yet).
    """
    reviews = Review.objects.select_related("lead", "lead__contact").order_by("-created_at")

    rated = Q(rating__isnull=False)
    outstanding = Q(
        rating__isnull=True,
        delivery_status__in=[Review.DeliveryStatus.PENDING, Review.DeliveryStatus.SENT],
    )
    agg = reviews.aggregate(
        avg=Avg(
            "rating",
            filter=rated,
            output_field=DecimalField(max_digits=4, decimal_places=2),
        ),
        responses=Count("id", filter=rated),
        pending=Count("id", filter=outstanding),
    )
    stats = {"avg": agg["avg"], "responses": agg["responses"], "pending": agg["pending"]}

    avg_stars = 0
    if stats["avg"] is not None:
        avg_stars = int(stats["avg"].to_integral_value(rounding=ROUND_HALF_UP))

    return render(
        request,
        "messaging/review_list.html",
        {
            "nav": "reviews",
            "page_title": "Reviews",
            "reviews": reviews,
            "stats": stats,
            "avg_stars": avg_stars,
        },
    )
