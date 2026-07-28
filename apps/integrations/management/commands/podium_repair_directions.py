"""Re-derive message direction + sender from stored Podium webhook payloads.

Messages ingested before `metadata.eventType` was read correctly all landed as
inbound, because the event-type lookup missed and fell through to a
"message.received" default. The raw payload is kept on every PodiumEvent, so the
correct direction is recoverable without re-fetching anything from Podium.

Safe to re-run: each event is re-derived from its payload, not from current state.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.integrations import podium
from apps.integrations.models import PodiumEvent
from apps.integrations.webhooks import podium_event_type
from apps.messaging.models import Message


class Command(BaseCommand):
    help = "Repair message direction/sender from stored Podium webhook payloads."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        names = podium.user_name_map()
        if not names:
            self.stdout.write(
                self.style.WARNING(
                    "No Podium user names resolved — agent messages will be repaired "
                    "without a sender name. Check the read_users scope."
                )
            )

        repaired = skipped = unchanged = 0

        for event in PodiumEvent.objects.order_by("id").iterator():
            payload = event.payload or {}
            data = payload.get("data") or {}
            msg_uid = data.get("uid") or ""
            event_type = podium_event_type(payload)

            if not msg_uid or event_type not in {
                PodiumEvent.EventType.MESSAGE_RECEIVED,
                PodiumEvent.EventType.MESSAGE_SENT,
            }:
                continue

            message = Message.objects.filter(podium_message_uid=msg_uid).first()
            if message is None:
                skipped += 1
                continue

            if event_type == PodiumEvent.EventType.MESSAGE_SENT:
                sender_uid = data.get("senderUid") or (data.get("sender") or {}).get("uid") or ""
                target = {
                    "direction": Message.Direction.OUT,
                    "delivery_status": Message.DeliveryStatus.SENT,
                    "podium_sender_uid": sender_uid,
                    "sender_name": names.get(sender_uid, "") if sender_uid else "",
                }
                if message.sent_at is None:
                    target["sent_at"] = message.created_at or timezone.now()
            else:
                contact_data = data.get("contact") or {}
                target = {
                    "direction": Message.Direction.IN,
                    "delivery_status": Message.DeliveryStatus.RECEIVED,
                    "podium_sender_uid": "",
                    "sender_name": (
                        contact_data.get("name") or data.get("contactName") or ""
                    ).strip(),
                }

            changes = {f: v for f, v in target.items() if getattr(message, f) != v}
            # An already-correct row still needs its event_type fixed below.
            if changes and not dry_run:
                for field, value in changes.items():
                    setattr(message, field, value)
                message.save(update_fields=[*changes, "updated_at"])

            if event.event_type != event_type and not dry_run:
                event.event_type = event_type
                event.save(update_fields=["event_type", "updated_at"])

            if changes:
                repaired += 1
                verb = "would repair" if dry_run else "repaired"
                self.stdout.write(
                    f"  {verb} {msg_uid}: {', '.join(sorted(changes))} "
                    f"({message.body[:40].strip()!r})"
                )
            else:
                unchanged += 1

        prefix = "DRY RUN — " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}{repaired} repaired · {unchanged} already correct · "
                f"{skipped} events with no matching message"
            )
        )
