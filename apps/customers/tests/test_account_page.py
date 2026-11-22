"""The Account page, `/account/settings/`: name, mobile and password."""

from django.contrib.contenttypes.models import ContentType
from django.urls import reverse

from apps.audit.models import AuditEntry
from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.contacts.models import Contact
from apps.customers import services
from apps.customers.factories import CustomerAccountFactory

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
    assert "Contact us to change this" in html
    assert 'name="email"' not in html and 'name="company"' not in html


def test_a_contact_with_no_company_has_no_company_row(client_as):
    html = client_as(_account(email="dana@example.com", company=None)).get(URL).content.decode()
    assert ">Company<" not in html and "None" not in html
    assert "dana@example.com" in html


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


def test_password_change_needs_current_password(client_as):
    account = _account()
    response = client_as(account).post(URL, _password(current_password="not it"))
    assert response.status_code == 200
    assert "Current password is incorrect." in response.content.decode()
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
    assert "at least 10 characters" in response.content.decode()
    account.refresh_from_db()
    assert account.check_password(CURRENT)


def test_a_password_like_the_email_is_refused(client_as):
    account = _account(email="dana.whitfield@example.com")
    pw = "dana.whitfield"
    response = client_as(account).post(URL, _password(new_password1=pw, new_password2=pw))
    assert response.status_code == 200
    account.refresh_from_db()
    assert account.check_password(CURRENT)


def test_mismatched_confirmation_is_refused(client_as):
    account = _account()
    response = client_as(account).post(URL, _password(new_password2=NEW + "x"))
    assert response.status_code == 200
    account.refresh_from_db()
    assert account.check_password(CURRENT)


def test_the_service_enforces_ten_characters(account):
    for short in ("abcdefg-9", "xq7-zv4-k"):
        try:
            services.change_password(account, CURRENT, short)
        except services.CustomerError:
            continue
        raise AssertionError("a short password was accepted")


def test_an_unknown_action_is_a_bad_request(client_as):
    assert client_as(_account()).post(URL, {"action": "nope"}).status_code == 400
