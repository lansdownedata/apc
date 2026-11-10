import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.leads.factories import LeadFactory
from apps.leads.forms import NewLeadForm

pytestmark = pytest.mark.django_db


def test_lead_update_stores_e164(client):
    lead = LeadFactory(channel="website")
    client.force_login(UserFactory())

    response = client.post(reverse("lead_update", args=[lead.pk]), {"phone": "(617) 555-0207"})

    assert response.status_code == 200
    lead.contact.refresh_from_db()
    assert lead.contact.phone == "+16175550207"


def test_lead_update_rejects_invalid_phone(client):
    lead = LeadFactory(channel="website")
    lead.contact.phone = "+12025550100"
    lead.contact.save(update_fields=["phone"])
    client.force_login(UserFactory())

    response = client.post(reverse("lead_update", args=[lead.pk]), {"phone": "12345"})

    assert response.status_code == 400
    assert "valid phone" in response.json()["error"]
    lead.contact.refresh_from_db()
    assert lead.contact.phone == "+12025550100", "nothing may be written when validation fails"


def test_lead_update_allows_blank_phone(client):
    lead = LeadFactory(channel="website")
    client.force_login(UserFactory())

    response = client.post(reverse("lead_update", args=[lead.pk]), {"phone": ""})

    assert response.status_code == 200
    lead.contact.refresh_from_db()
    assert lead.contact.phone == ""


def test_lead_update_without_phone_key_leaves_it_alone(client):
    lead = LeadFactory(channel="website")
    lead.contact.phone = "+16175550207"
    lead.contact.save(update_fields=["phone"])
    client.force_login(UserFactory())

    response = client.post(reverse("lead_update", args=[lead.pk]), {"name": "Ada"})

    assert response.status_code == 200
    lead.contact.refresh_from_db()
    assert lead.contact.phone == "+16175550207"


def test_new_lead_form_normalizes_phone():
    form = NewLeadForm({"name": "Ada", "phone": "(617) 555-0207", "channel": "website"})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["phone"] == "+16175550207"


def test_new_lead_form_rejects_invalid_phone():
    form = NewLeadForm({"name": "Ada", "phone": "12345", "channel": "website"})
    assert not form.is_valid()
    assert "phone" in form.errors


def test_new_lead_form_allows_blank_phone():
    form = NewLeadForm({"name": "Ada", "phone": "", "channel": "website"})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["phone"] == ""


def test_lead_update_rejects_duplicate_email(client):
    from apps.contacts.factories import ContactFactory

    ContactFactory(email="taken@example.com")
    lead = LeadFactory()
    client.force_login(UserFactory())
    resp = client.post(reverse("lead_update", args=[lead.pk]), {"email": "TAKEN@example.com"})
    assert resp.status_code == 400
    assert "email" in resp.json()["error"].lower()


# --- LEAD-03: the workspace header's phone input -------------------------------------
# The header phone input calls `onPhoneBlur($event)` on the quoteWorkspace component.
# cc0e3f3 removed that method from quoteWorkspace (it survived only on contactProfile),
# so tabbing out threw "onPhoneBlur is not defined" and nothing saved. There is no JS test
# runner in the repo; these run the component's source under node with the browser
# globals stubbed.

ROOT = Path(__file__).resolve().parents[3]


def _quote_workspace_source() -> str:
    app_js = (ROOT / "static" / "js" / "app.js").read_text()
    start = app_js.index("function quoteWorkspace(")
    return app_js[start : app_js.index("window.quoteWorkspace = quoteWorkspace;", start)]


def test_header_phone_input_calls_a_method_quote_workspace_defines():
    html = (ROOT / "templates" / "leads" / "lead_detail.html").read_text()
    phone_input = re.search(r'<input type="tel" data-phone[^>]*>', html).group(0)
    handler = re.search(r'@blur="(\w+)\(', phone_input).group(1)
    assert re.search(rf"^\s+{handler}\(", _quote_workspace_source(), re.M)


_HARNESS = """
const vm = require("vm");
const fs = require("fs");
const posted = [];
const ctx = {
  URLSearchParams,
  phoneIsValid: () => true,
  phoneValue: () => "+16175550207",
  getCookie: () => "csrf",
  Alpine: { store: () => ({ push() {} }) },
  fetch: (url, o) => { posted.push([url, String(o.body)]); return new Promise(() => {}); },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(0, "utf8") + `
  ws = quoteWorkspace({ updateUrl: "/u/", header: { name: "Ada", phone: "" } });
  ws._saved = { ...ws.header };
  ws.onPhoneBlur({ target: { value: "(617) 555-0207", classList: { add() {}, remove() {} } } });
`, ctx);
process.stdout.write(JSON.stringify(posted));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_header_phone_blur_posts_only_the_new_phone_to_lead_update():
    result = subprocess.run(
        ["node", "-e", _HARNESS],
        input=_quote_workspace_source(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [["/u/", "phone=%2B16175550207"]]
