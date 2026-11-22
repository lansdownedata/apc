"""The Account page, `/account/settings/`: name, mobile and password."""

import re
from datetime import timedelta

import pytest
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditEntry
from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.contacts.models import Contact
from apps.customers import services
from apps.customers.factories import CustomerAccountFactory

pytestmark = pytest.mark.django_db

URL = "/account/settings/"
CURRENT = "correct horse battery"
NEW = "a-long-pass-phrase-77"


def _account(**contact):
    return CustomerAccountFactory(contact=ContactFactory(**contact))


def _details(**kw):
    return {"action": "details", "name": "Dana Whitfield", "mobile": "(703) 555-0188", **kw}


def _password(**kw):
    return {
        "action": "password",
        "current_password": CURRENT,
        "new_password1": NEW,
        "new_password2": NEW,
        **kw,
    }


def test_the_account_route_and_nav_link_exist(client_as):
    response = client_as(_account()).get(reverse("customers:account"))
    assert response.status_code == 200
    assert reverse("customers:account") == URL
    assert f'href="{URL}"' in response.content.decode()
    assert "Your details" in response.content.decode()


def test_the_account_page_needs_sign_in(client):
    assert client.get(URL).status_code == 302


def test_name_and_mobile_update_the_contact(client_as):
    account = _account(phone="")
    response = client_as(account).post(URL, _details())
    assert response.status_code == 302
    contact = Contact.objects.get(pk=account.contact_id)
    assert contact.name == "Dana Whitfield"
    assert contact.phone == "+17035550188"


def test_a_new_mobile_becomes_the_texting_number(client_as):
    account = _account(phone="+15715550100")
    client_as(account).post(URL, _details())
    contact = Contact.objects.get(pk=account.contact_id)
    assert contact.phone == "+17035550188"
    assert contact.phones.filter(texting=True).get().number == "+17035550188"


def test_the_edit_is_audited_as_the_customer_portal(client_as):
    account = _account()
    client_as(account).post(URL, _details())
    entries = AuditEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(Contact), object_id=account.contact_id
    ).exclude(action=AuditEntry.Action.CREATE)
    assert entries and {entry.source for entry in entries} == {"customer portal"}


def test_email_and_company_cannot_be_posted(client_as):
    company = CompanyFactory(name="Meridian Advisory Group")
    account = _account(email="dana@example.com", company=company)
    client_as(account).post(URL, _details(email="other@example.com", company="Someone Else"))
    contact = Contact.objects.get(pk=account.contact_id)
    assert contact.email == "dana@example.com"
    assert contact.company_id == company.pk
    assert contact.name == "Dana Whitfield"


def test_the_page_shows_email_and_company_as_read_only(client_as):
    company = CompanyFactory(name="Meridian Advisory Group")
    account = _account(email="dana@example.com", company=company)
    html = client_as(account).get(URL).content.decode()
    assert "dana@example.com" in html and "Meridian Advisory Group" in html
    # said once for both, not under each
    assert html.count("Contact us to change your email or company:") == 1
    assert html.count("Contact us to change") == 1
    assert 'name="email"' not in html and 'name="company"' not in html


def test_a_contact_with_no_company_has_no_company_row(client_as):
    html = client_as(_account(email="dana@example.com", company=None)).get(URL).content.decode()
    assert ">Company<" not in html and "None" not in html
    assert "dana@example.com" in html
    assert "Contact us to change your email:" in html and "or company" not in html


def test_one_primary_button_and_one_plain(client_as):
    html = client_as(_account()).get(URL).content.decode()
    save = re.search(r'<button type="submit" class="([^"]*)">Save changes</button>', html)[1]
    change = re.search(r'<button type="submit" class="([^"]*)">Change password</button>', html)[1]
    assert "btn-gold" in save and "btn-gold" not in change
    for classes in (save, change):
        assert "focus-gold" in classes


def test_a_field_error_is_tied_to_its_input(client_as):
    html = client_as(_account()).post(URL, _details(mobile="12")).content.decode()
    field = re.search(r'<input id="acc-mobile"[^>]*>', html)[0]
    assert 'aria-invalid="true"' in field and 'aria-describedby="acc-mobile-error"' in field
    assert '<p id="acc-mobile-error"' in html
    clean = client_as(_account()).get(URL).content.decode()
    assert "aria-invalid" not in clean and "acc-mobile-error" not in clean


def test_a_name_keeps_no_control_characters(client_as):
    account = _account()
    client_as(account).post(URL, _details(name="Dana\t\x1b[31m  Whitfield\r\n"))
    assert Contact.objects.get(pk=account.contact_id).name == "Dana [31m Whitfield"


# --- a number on file that can't be dialled (old or imported data) -----------------------


def test_a_stored_number_that_cannot_be_dialled_does_not_block_a_name_edit(client_as):
    account = _account(name="Before", phone="555-0100 ext 4")
    client = client_as(account)
    shown = re.search(
        r'<input id="acc-mobile"[^>]*value="([^"]*)"', client.get(URL).content.decode()
    )[1]
    assert shown == "555-0100 ext 4"
    response = client.post(URL, _details(name="Dana Whitfield", mobile=shown))
    assert response.status_code == 302
    contact = Contact.objects.get(pk=account.contact_id)
    assert (contact.name, contact.phone) == ("Dana Whitfield", "555-0100 ext 4")


def test_a_good_number_posted_back_untouched_changes_nothing(client_as):
    account = _account(phone="+15715550100")
    before = account.contact.phones.count()
    client_as(account).post(URL, _details(mobile="(571) 555-0100"))
    contact = Contact.objects.get(pk=account.contact_id)
    assert contact.phone == "+15715550100" and contact.phones.count() == before


def test_replacing_a_number_that_cannot_be_dialled_still_validates_the_new_one(client_as):
    account = _account(name="Before", phone="555-0100 ext 4")
    client = client_as(account)
    bad = client.post(URL, _details(mobile="555-0199 ext 4"))
    assert bad.status_code == 200 and "Enter a valid mobile number." in bad.content.decode()
    assert Contact.objects.get(pk=account.contact_id).name == "Before"
    client.post(URL, _details())
    assert Contact.objects.get(pk=account.contact_id).phone == "+17035550188"


def test_invalid_mobile_is_a_form_error(client_as):
    account = _account(name="Before")
    response = client_as(account).post(URL, _details(mobile="12"))
    assert response.status_code == 200
    html = response.content.decode()
    assert "Enter a valid mobile number." in html
    assert 'value="12"' in html
    assert Contact.objects.get(pk=account.contact_id).name == "Before"


def test_a_blank_name_is_refused(client_as):
    account = _account(name="Before")
    response = client_as(account).post(URL, _details(name="  "))
    assert response.status_code == 200
    assert "Enter your name." in response.content.decode()
    assert Contact.objects.get(pk=account.contact_id).name == "Before"


def test_a_blank_mobile_leaves_the_number_on_file(client_as):
    account = _account(phone="+15715550100")
    response = client_as(account).post(URL, _details(mobile=""))
    assert response.status_code == 302
    assert Contact.objects.get(pk=account.contact_id).phone == "+15715550100"


def test_saving_details_flashes_a_message(client_as):
    response = client_as(_account()).post(URL, _details(), follow=True)
    assert "Details saved." in response.content.decode()


def _error_under(html: str, field_id: str) -> str:
    """The error text tied to one input, or "" when it has none."""
    found = re.search(rf'<p id="{field_id}-error"[^>]*>([^<]*)</p>', html)
    return found[1] if found else ""


def test_password_change_needs_current_password(client_as):
    account = _account()
    response = client_as(account).post(URL, _password(current_password="not it"))
    assert response.status_code == 200
    html = response.content.decode()
    assert _error_under(html, "acc-current") == "Current password is incorrect."
    assert _error_under(html, "acc-new1") == ""
    account.refresh_from_db()
    assert account.check_password(CURRENT)


def test_wrong_current_password_counts_toward_the_lockout(client_as):
    account = _account()
    client = client_as(account)
    for _ in range(services.LOCKOUT_FAILURES):
        client.post(URL, _password(current_password="not it"))
    account.refresh_from_db()
    assert account.locked_until is not None
    # locked: even the right current password is refused, and the answer is the same
    response = client.post(URL, _password())
    assert "Current password is incorrect." in response.content.decode()
    account.refresh_from_db()
    assert account.check_password(CURRENT)


def test_password_change_keeps_this_session_and_ends_others(client_as):
    account = _account()
    this, other = client_as(account), client_as(account)
    response = this.post(URL, _password(), follow=True)
    assert "Password changed." in response.content.decode()
    account.refresh_from_db()
    assert account.check_password(NEW)
    assert this.get(URL).status_code == 200
    assert other.get(URL).status_code == 302


def test_weak_new_password_is_refused(client_as):
    account = _account()
    response = client_as(account).post(
        URL, _password(new_password1="short-9", new_password2="short-9")
    )
    assert response.status_code == 200
    html = response.content.decode()
    assert "at least 10 characters" in _error_under(html, "acc-new1")
    assert _error_under(html, "acc-current") == ""
    account.refresh_from_db()
    assert account.check_password(CURRENT)


def test_a_password_like_the_email_is_refused(client_as):
    account = _account(email="dana.whitfield@example.com")
    pw = "dana.whitfield"
    response = client_as(account).post(URL, _password(new_password1=pw, new_password2=pw))
    assert response.status_code == 200
    assert "too similar to the email" in _error_under(response.content.decode(), "acc-new1")
    account.refresh_from_db()
    assert account.check_password(CURRENT)


def test_mismatched_confirmation_is_refused(client_as):
    account = _account()
    response = client_as(account).post(URL, _password(new_password2=NEW + "x"))
    assert response.status_code == 200
    assert "didn’t match" in _error_under(response.content.decode(), "acc-new2")
    account.refresh_from_db()
    assert account.check_password(CURRENT)


def test_the_service_enforces_ten_characters(account):
    # nine characters that pass every other rule: only the length can be what refuses them
    for short in ("xq7-zv4-k", "Tr0ub4d&r"):
        with pytest.raises(services.CustomerError, match="at least 10 characters") as refused:
            services.change_password(account, CURRENT, short)
        assert refused.value.field == "new_password1"
    services.change_password(account, CURRENT, "xq7-zv4-kp")  # ten of the same kind pass
    account.refresh_from_db()
    assert account.check_password("xq7-zv4-kp")


def test_a_service_refusal_shows_under_the_field_it_is_about(client_as, monkeypatch):
    """The form and the service hold the same rules, so the service's own refusal is
    rarely reached; when it is, it reads under its field, not under Current password."""
    account = _account()

    def refuse(*args: object, **kwargs: object) -> None:
        raise services.CustomerError("Pick something longer.", field="new_password1")

    monkeypatch.setattr(services, "change_password", refuse)
    html = client_as(account).post(URL, _password()).content.decode()
    assert _error_under(html, "acc-new1") == "Pick something longer."
    assert _error_under(html, "acc-current") == ""


def test_a_service_refusal_about_no_field_shows_above_the_form(client_as, monkeypatch):
    def refuse(*args: object, **kwargs: object) -> None:
        raise services.CustomerError("Try again in a minute.")

    monkeypatch.setattr(services, "change_password", refuse)
    html = client_as(_account()).post(URL, _password()).content.decode()
    assert (
        '<p role="alert" class="mt-3 text-[12.5px] text-rose-700">Try again in a minute.</p>'
        in html
    )
    assert _error_under(html, "acc-current") == ""


def test_changing_the_password_clears_the_failed_count_and_a_stale_lock(client_as):
    account = _account()
    client = client_as(account)
    for _ in range(3):
        client.post(URL, _password(current_password="not it"))
    account.refresh_from_db()
    assert account.failed_logins == 3
    # a lock that has run out but was never cleared (nobody signed in since)
    type(account).objects.filter(pk=account.pk).update(
        locked_until=timezone.now() - timedelta(minutes=1)
    )
    assert client.post(URL, _password()).status_code == 302
    account.refresh_from_db()
    assert (account.failed_logins, account.locked_until) == (0, None)


def test_an_unknown_action_is_a_bad_request(client_as):
    assert client_as(_account()).post(URL, {"action": "nope"}).status_code == 400
