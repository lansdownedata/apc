from django.db import migrations


def forwards(apps, schema_editor):
    """Give every existing message a conversation, derived from its lead's contact."""
    Message = apps.get_model("messaging", "Message")
    Conversation = apps.get_model("messaging", "Conversation")
    for message in Message.objects.select_related("lead").iterator():
        if message.lead_id is None:
            continue
        conversation, _ = Conversation.objects.get_or_create(contact_id=message.lead.contact_id)
        Message.objects.filter(pk=message.pk).update(conversation=conversation)
        if (
            conversation.last_message_at is None
            or conversation.last_message_at < message.created_at
        ):
            conversation.last_message_at = message.created_at
            conversation.save(update_fields=["last_message_at"])


def backwards(apps, schema_editor):
    Message = apps.get_model("messaging", "Message")
    Message.objects.update(conversation=None)


class Migration(migrations.Migration):
    dependencies = [
        ("messaging", "0005_message_conversation_alter_message_lead"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
