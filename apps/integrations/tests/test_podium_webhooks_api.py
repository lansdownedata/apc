from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from apps.integrations import podium
from apps.integrations.factories import PodiumCredentialFactory

pytestmark = pytest.mark.django_db


def _fresh():
    return PodiumCredentialFactory(
        access_token="AT", expires_at=timezone.now() + timedelta(hours=5)
    )


def test_create_webhook_posts():
    _fresh()
    fake = MagicMock(content=b"{}")
    fake.json.return_value = {"uid": "wh-1"}
    with patch.object(podium.requests, "request", return_value=fake) as req:
        out = podium.create_webhook(
            url="https://x/webhooks/podium/",
            event_types=["message.received", "message.sent"],
            secret="s3cr3t",
            organization_uid="org-1",
        )
    method, url = req.call_args.args
    kwargs = req.call_args.kwargs
    assert method == "POST"
    assert url.endswith("/v4/webhooks")
    assert kwargs["json"]["eventTypes"] == ["message.received", "message.sent"]
    assert kwargs["json"]["url"] == "https://x/webhooks/podium/"
    assert kwargs["json"]["secret"] == "s3cr3t"
    assert kwargs["json"]["organizationUid"] == "org-1"
    assert out == {"uid": "wh-1"}


def test_list_webhooks_gets():
    _fresh()
    fake = MagicMock(content=b"{}")
    fake.json.return_value = {"data": []}
    with patch.object(podium.requests, "request", return_value=fake) as req:
        podium.list_webhooks()
    method, url = req.call_args.args
    assert method == "GET"
    assert url.endswith("/v4/webhooks")


def test_delete_webhook_deletes():
    _fresh()
    with patch.object(podium.requests, "request", return_value=MagicMock(content=b"")) as req:
        podium.delete_webhook("wh-1")
    method, url = req.call_args.args
    assert method == "DELETE"
    assert url.endswith("/v4/webhooks/wh-1")
