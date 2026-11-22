"""The customer portal, `/account/`: sign in, sign up, passwords, and the pages behind them.

Never `@login_required`: customers have their own session (`apps.customers.auth`) and never
set `request.user`. Modelled on `apps/planners/views.py`, without the texted code: a
customer signs in with a password and a verified email.
"""

from __future__ import annotations

import posixpath
from dataclasses import asdict
from datetime import timedelta
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit

from django.contrib import messages
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core import signing
from django.core.exceptions import ValidationError
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseNotAllowed,
    JsonResponse,
    QueryDict,
)
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import Resolver404, resolve, reverse
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_decode
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from apps.audit.context import audit_context, current_cid
from apps.audit.middleware import source_for
from apps.core import throttle
from apps.core.net import client_ip
from apps.core.phone import to_e164
from apps.core.templatetags.phone_filters import phone_display
from apps.planners.selectors import dispatch_phone

from . import exports, receipts, requests, selectors, services, trip_detail, trip_list
from . import home as home_page
from .auth import FRAGMENT_PARAM, current_customer, customer_required, sign_in, sign_out
from .forms import (
    RIDERS,
    ChangePasswordForm,
    CustomerPasswordForm,
    DetailsForm,
    SignupForm,
    TripRequestForm,
)
from .models import CustomerAccount
from .tokens import customer_reset_token, customer_token

# Per-IP brakes: a brake on guessing, not a lock. Counted in the database (`throttle`), so
# each limit is one limit however many web workers answer. Sign-up mails count against the
# reset limit.
LOGIN_FAILURE_LIMIT = 10
LOGIN_FAILURE_WINDOW = timedelta(minutes=15)
RESET_LIMIT = 5
RESET_WINDOW = timedelta(hours=1)

# Like Django's PasswordResetConfirmView: a link's token is moved into the session and the
# address bar shows this placeholder instead, so it never sits in history or a Referer.
TOKEN_PLACEHOLDER = "set"
LINK_SESSION_KEY = "_customer_link_token"
SIGNUP_SESSION_KEY = "_customer_signup_token"


def _page(
    request: HttpRequest, template: str, context: dict | None = None, status: int = 200
) -> HttpResponse:
    """Render a customer page. The shell shows the dispatch number on every one, and the
    portal nav only behind `customer_required`."""
    context = {
        "dispatch": dispatch_phone(),
        "signed_in": hasattr(request, "customer"),
        "can_request": hasattr(request, "customer") and requests.can_request(request.customer),
        **(context or {}),
    }
    return render(request, template, context, status=status)


def _over(request: HttpRequest, scope: str, limit: int) -> bool:
    return throttle.over(scope, client_ip(request), limit)


def _count(request: HttpRequest, scope: str, window: timedelta) -> None:
    throttle.hit(scope, client_ip(request), window)


def _home() -> str:
    return reverse("customers:home")


# The signed-in pages: the only places `?next=` may land. Every other route under
# `/account/` is a form's target (it answers 405 to the GET a redirect makes), a download,
# or a signed-out page. `test_next_lands_only_on_pages` holds this list to the routes.
LANDING_PAGES = frozenset({"home", "trips", "trip", "account", "request_trip"})


def _is_landing_page(target: str) -> bool:
    try:
        match = resolve(unquote(urlsplit(target).path))
    except Resolver404:
        return False
    return match.namespace == "customers" and match.url_name in LANDING_PAGES


def _safe_next(request: HttpRequest) -> str:
    """`?next=` when it is a same-host path to one of the portal's own pages
    (`LANDING_PAGES`), with its query string; Home otherwise.

    The path is checked as a browser will resolve it: percent-decoded (`%2e` is `.`), `\\`
    read as `/`, then `..` applied, so `/account/%2e%2e/portal/` is refused. A `fragment`
    parameter is dropped: that is a piece of a page, never somewhere to land."""
    target = _without_fragment(request.POST.get("next") or request.GET.get("next") or "")
    path = unquote(urlsplit(target).path).replace("\\", "/")
    path = posixpath.normpath(path) + "/"
    if (
        target.startswith(_home())
        and path.startswith(_home())
        and url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        )
        and _is_landing_page(target)
    ):
        return target
    return _home()


def _without_fragment(target: str) -> str:
    parts = urlsplit(target)
    query = QueryDict(parts.query, mutable=True)
    if FRAGMENT_PARAM not in query:
        return target
    query.pop(FRAGMENT_PARAM)
    return urlunsplit(parts._replace(query=query.urlencode()))


def _link_invalid(request: HttpRequest, message: str = "") -> HttpResponse:
    return _page(request, "customers/link_invalid.html", {"message": message}, status=400)


# --- sign in / out ---------------------------------------------------------------------


@sensitive_post_parameters("password")
def login(request: HttpRequest) -> HttpResponse:
    """A customer already signed in (the public "Customer login" link is how returning
    customers arrive) goes straight on, to a safe `next` or Home."""
    if current_customer(request) is not None:
        return redirect(_safe_next(request))
    context = {"next": request.POST.get("next") or request.GET.get("next") or ""}
    if request.method == "POST":
        if _over(request, "login", LOGIN_FAILURE_LIMIT):
            return _page(request, "customers/login.html", {**context, "throttled": True}, 429)
        email = request.POST.get("email", "")
        account = services.authenticate(email, request.POST.get("password", ""))
        if account is not None:
            sign_in(request, account)
            return redirect(_safe_next(request))
        _count(request, "login", LOGIN_FAILURE_WINDOW)
        context.update(error=True, email=email)
    return _page(request, "customers/login.html", context)


@require_POST
def logout(request: HttpRequest) -> HttpResponse:
    sign_out(request)
    messages.success(request, "You're signed out.")
    return redirect("customers:login")


# --- sign up ---------------------------------------------------------------------------


def signup(request: HttpRequest) -> HttpResponse:
    """Step 1, then step 2 ("Check your email") with the typed email, whatever happened to
    it. Only a site-wide refusal (sending off, no public address) shows here instead. A
    customer already signed in goes straight on, as from the sign-in page."""
    if current_customer(request) is not None:
        return redirect(_safe_next(request))
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        if not _over(request, "reset", RESET_LIMIT):
            _count(request, "reset", RESET_WINDOW)
            try:
                services.start_signup(name=data["name"], email=data["email"], phone=data["phone"])
            except services.CustomerError as exc:
                return _page(request, "customers/signup.html", {"form": form, "error": str(exc)})
        return _page(
            request,
            "customers/signup_sent.html",
            {
                "email": data["email"].strip().lower(),
                "form": form,
                "link_days": services.SIGNUP_MAX_AGE.days,
            },
        )
    return _page(request, "customers/signup.html", {"form": form})


def _hop(request: HttpRequest, session_key: str, token: str, url: str) -> HttpResponse:
    request.session[session_key] = token
    return redirect(url)


@sensitive_post_parameters("new_password1", "new_password2")
def signup_confirm(request: HttpRequest, token: str) -> HttpResponse:
    """Step 3 from a sign-up link: choose a password, then the account exists and the
    customer is signed in."""
    try:
        data = services.read_signup(
            request.session.get(SIGNUP_SESSION_KEY, "") if token == TOKEN_PLACEHOLDER else token
        )
        # A used link, or a closed account's, is refused before it asks for a password.
        services.check_signup(data)
    except signing.BadSignature:
        return _link_invalid(request)
    except services.CustomerError as exc:
        return _link_invalid(request, str(exc))
    if token != TOKEN_PLACEHOLDER:
        url = reverse("customers:signup_confirm", args=[TOKEN_PLACEHOLDER])
        return _hop(request, SIGNUP_SESSION_KEY, token, url)
    form = CustomerPasswordForm(services.signup_stand_in(data), request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            # History names the person signing up: nobody is signed in yet, but the link
            # proved the address.
            with audit_context(
                source=source_for(request), actor_email=data.get("email", ""), cid=current_cid()
            ):
                account = services.complete_signup(data, form.cleaned_data["new_password1"])
        except services.CustomerError as exc:
            return _link_invalid(request, str(exc))
        request.session.pop(SIGNUP_SESSION_KEY, None)
        sign_in(request, account)
        messages.success(request, "You're all set.")
        return redirect(_home())
    return _page(
        request,
        "customers/set_password.html",
        {"form": form, "email": data.get("email", ""), "signup": True},
    )


# --- set a password from an emailed link -----------------------------------------------


def _account_from_link(
    uidb64: str, token: str, generator: PasswordResetTokenGenerator
) -> CustomerAccount | None:
    try:
        pk = force_str(urlsafe_base64_decode(uidb64))
        account = CustomerAccount.objects.select_related("contact").get(pk=pk)
    except (CustomerAccount.DoesNotExist, ValueError, TypeError, OverflowError, ValidationError):
        return None
    if account.status == CustomerAccount.Status.REVOKED:
        return None
    return account if generator.check_token(account, token) else None


@sensitive_post_parameters("new_password1", "new_password2")
def set_password(request: HttpRequest, uidb64: str, token: str) -> HttpResponse:
    """The invite (`register`) and password-reset (`set_password`) links share this page.
    Unauthenticated by design: the token is the guard, and it dies once a password is set."""
    name = request.resolver_match.url_name
    generator = customer_reset_token if name == "set_password" else customer_token
    if token != TOKEN_PLACEHOLDER:
        if _account_from_link(uidb64, token, generator) is None:
            return _link_invalid(request)
        url = reverse(f"customers:{name}", kwargs={"uidb64": uidb64, "token": TOKEN_PLACEHOLDER})
        return _hop(request, LINK_SESSION_KEY, token, url)
    account = _account_from_link(uidb64, request.session.get(LINK_SESSION_KEY, ""), generator)
    if account is None:
        return _link_invalid(request)
    first_time = account.registered_at is None
    form = CustomerPasswordForm(account, request.POST or None)
    if request.method == "POST" and form.is_valid():
        services.register(account, form.cleaned_data["new_password1"])
        request.session.pop(LINK_SESSION_KEY, None)
        sign_in(request, account)
        messages.success(request, "You're all set." if first_time else "Password updated.")
        return redirect(_home())
    return _page(
        request,
        "customers/set_password.html",
        {"form": form, "email": account.email, "reset": not first_time},
    )


def password_reset(request: HttpRequest) -> HttpResponse:
    """Forgot password. The same answer whoever asked, so it never confirms an account."""
    email = ""
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        if not _over(request, "reset", RESET_LIMIT):
            _count(request, "reset", RESET_WINDOW)
            services.send_password_reset(email)
    return _page(
        request,
        "customers/password_reset.html",
        {
            "sent": request.method == "POST",
            "email": email,
            "link_days": customer_reset_token.timeout.days,
        },
    )


# --- signed in -------------------------------------------------------------------------


@customer_required
def home(request: HttpRequest) -> HttpResponse:
    """Next trip, what is waiting on the customer, what is coming up. The history banner
    is decided on the first Home visit only: shown then if history was found, and stamped
    seen either way, so a trip completed in the portal later is never announced as found."""
    account = request.customer
    context = home_page.home(account)
    if account.history_banner_seen_at is None:
        # A queryset update, as `auth._log_view`: the stamp is housekeeping the trail
        # leaves out, so a save would only cost History's read of the row before it.
        account.history_banner_seen_at = timezone.now()
        CustomerAccount.objects.filter(pk=account.pk).update(
            history_banner_seen_at=account.history_banner_seen_at
        )
    return _page(request, "customers/home.html", context)


def _export_query(f: trip_list.TripFilter) -> str:
    """The filter as the export link carries it (no paging, no grouping)."""
    params = {
        "tab": f.tab,
        "start": f.start.isoformat() if f.start else "",
        "end": f.end.isoformat() if f.end else "",
        "q": f.q,
        "passenger": f.passenger,
    }
    return urlencode({key: value for key, value in params.items() if value})


@customer_required
def trips(request: HttpRequest) -> HttpResponse:
    """Trips and history. The page is always the first page of rows. `?fragment=1` returns
    the table alone, which the page fetches when a filter changes; with `page` past 1 it
    returns that page's rows only, which "Show more" appends."""
    f = trip_list.TripFilter.from_query(request.GET)
    fragment = bool(request.GET.get("fragment"))
    if fragment and f.page > 1:
        context = trip_list.page_of(request.customer, f)
        return _page(request, "customers/_trips_more.html", context)
    f = f.first_page()
    context = {
        **trip_list.trips(request.customer, f),
        "filter": {
            **asdict(f),
            "start": f.start.isoformat() if f.start else "",
            "end": f.end.isoformat() if f.end else "",
        },
        "export_query": _export_query(f),
    }
    if fragment:
        return _page(request, "customers/_trips_table.html", context)
    return _page(
        request,
        "customers/trips.html",
        {
            **context,
            "passengers": trip_list.passenger_options(request.customer),
            "ranges": trip_list.quick_ranges(),
            "tabs": trip_list.TABS,
            "groupings": trip_list.GROUPINGS,
        },
    )


@customer_required
def trip(request: HttpRequest, pk: int) -> HttpResponse:
    """One trip (mockup 3). Outside the account's scope it does not exist: 404."""
    found = selectors.trip_or_404(request.customer, pk)
    return _page(
        request,
        "customers/trip.html",
        {
            "trip": trip_detail.trip_detail(request.customer, found),
            "reference_url": reverse("customers:set_reference"),
            "change_topics": requests.CHANGE_TOPICS,
            "cancel_reasons": [(reason, reason) for reason in requests.CANCEL_REASONS],
        },
    )


def _ask_error(reason: str) -> str:
    """What the dialog says when `requests.ask` refuses, above what the customer typed."""
    dispatch = dispatch_phone()["display"]
    return {
        "blank": "Tell us what needs to change, then send it.",
        "too_long": f"Please keep it under {requests.ASK_MAX_LENGTH:,} characters.",
        "limited": "You've sent a lot of requests in the last hour. "
        f"Please call dispatch at {dispatch}.",
        "closed": f"This trip can't take requests online any more. Please call dispatch at "
        f"{dispatch}.",
    }[reason]


# The toast on the trip page once a request is sent.
_ASK_SENT = {
    "change": "Request sent. We'll confirm the change with you.",
    "cancel": "Request sent. We'll confirm the cancellation by email.",
}


def _ask(request: HttpRequest, pk: int, kind: str) -> JsonResponse:
    """Mockups 6 and 7: the dialog posts here with `fetch` and reloads the trip page on
    `{"ok": true}`, which shows the toast and the pending state. A refusal answers 400
    with the sentence to show inside the dialog. Outside the account's scope: 404."""
    found = selectors.trip_or_404(request.customer, pk)
    try:
        requests.ask(
            request.customer,
            found,
            kind=kind,
            topics=request.POST.getlist("topics"),
            reason=request.POST.get("reason", ""),
            body=request.POST.get("body", ""),
        )
    except requests.RequestRefused as exc:
        return JsonResponse({"ok": False, "error": _ask_error(exc.reason)}, status=400)
    messages.success(request, _ASK_SENT[kind])
    return JsonResponse({"ok": True})


@customer_required
@require_POST
def ask_change(request: HttpRequest, pk: int) -> JsonResponse:
    return _ask(request, pk, "change")


@customer_required
@require_POST
def ask_cancel(request: HttpRequest, pk: int) -> JsonResponse:
    return _ask(request, pk, "cancel")


@customer_required
def receipt(request: HttpRequest, pk: int) -> HttpResponse:
    """The trip receipt PDF (mockup 4). Outside the account's scope, or before the trip has
    one (`receipts.has_receipt`): 404."""
    found = selectors.trip_or_404(request.customer, pk)
    if not receipts.has_receipt(found):
        raise Http404
    response = HttpResponse(
        receipts.render_pdf(receipts.receipt_context(found)), content_type="application/pdf"
    )
    filename = f"All-Pro-Charter-{found.reference}.pdf"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# Ticked rows are at most the rows a page has loaded; this keeps a hand-built post sane.
MAX_TICKED_IDS = 1000


def _ids(values: list[str]) -> list[int]:
    """Trip ids from `ids` sent repeated or comma-separated; anything but a plain number of
    up to 18 digits (a BIGINT) is dropped. At most `MAX_TICKED_IDS` are kept."""
    parts = (part.strip() for value in values for part in value.split(","))
    ids = [int(part) for part in parts if part.isascii() and part.isdigit() and len(part) <= 18]
    return ids[:MAX_TICKED_IDS]


@customer_required
@require_POST
def set_reference(request: HttpRequest) -> JsonResponse:
    """Set "Your reference" on ticked trips (or one). Ids outside the account are ignored.
    With `rows`, the answer carries those trips' table rows redrawn (`rows`, as
    customers/_trips_more.html), for the Trips page to swap in without refetching."""
    ids = _ids(request.POST.getlist("ids"))
    changed = requests.set_reference(request.customer, ids, request.POST.get("value", ""))
    answer = {"ok": True, "changed": changed}
    if request.POST.get("rows"):
        context = {"groups": trip_list.rows_for(request.customer, ids), "has_more": False}
        answer["rows"] = render_to_string("customers/_trips_more.html", context, request)
    return JsonResponse(answer)


def _quote_by(account: CustomerAccount) -> str:
    """How the quote will reach the customer: a text goes only to a mobile on file that can
    be dialled, so nobody without one is promised a text."""
    return "email and text" if to_e164(account.contact.phone) else "email"


@customer_required
def request_trip(request: HttpRequest) -> HttpResponse:
    """Request a trip (mockup 5). A valid post becomes a New lead for staff to price, and
    the customer lands on the new trip's page; an invalid one comes back with what they
    typed and the errors beside the fields."""
    account = request.customer
    if not requests.can_request(account):
        return _page(request, "customers/request.html", {"refused": True})
    needs_mobile = requests.needs_mobile(account)
    form = TripRequestForm(request.POST or None, needs_mobile=needs_mobile)
    if request.method == "POST" and form.is_valid():
        try:
            lead = requests.request_trip(account, form.cleaned_data)
        except services.CustomerError:
            form.add_error(
                None,
                "You've sent a lot of requests in the last hour. "
                f"Please call dispatch at {dispatch_phone()['display']}.",
            )
        else:
            trip = lead.reservations.order_by("pk").first()
            account.contact.refresh_from_db(fields=["phone"])
            by = _quote_by(account)
            messages.success(request, f"Request received. Your quote will arrive by {by}.")
            return redirect("customers:trip", pk=trip.pk)
    contact = account.contact
    return _page(
        request,
        "customers/request.html",
        {
            "form": form,
            "occasion_options": form.occasion_options(),
            "vehicle_options": form.vehicle_options(),
            "riders": RIDERS,
            "stops": form.posted_stops() if form.is_bound else [],
            "recent": requests.recent_addresses(account),
            "needs_mobile": needs_mobile,
            "quote_by": _quote_by(account),
            "requester": {
                "name": contact.name,
                "email": account.email,
                "phone": contact.phone,
            },
        },
    )


@customer_required
def export(request: HttpRequest) -> HttpResponse:
    """The Trips spreadsheet: GET with the page's filter, or POST with the ticked `ids`
    (a plain form post so the browser downloads it). Ids outside the account are dropped."""
    if request.method == "POST":
        ids = _ids(request.POST.getlist("ids"))
        text = exports.trips_csv(request.customer, trip_ids=ids)
    elif request.method == "GET":
        text = exports.trips_csv(request.customer, f=trip_list.TripFilter.from_query(request.GET))
    else:
        return HttpResponseNotAllowed(["GET", "POST"])
    response = HttpResponse(
        "\ufeff" + text,
        content_type="text/csv; charset=utf-8",  # BOM: Excel reads UTF-8
    )
    filename = f"all-pro-charter-trips-{timezone.localdate():%Y-%m-%d}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@customer_required
@sensitive_post_parameters("current_password", "new_password1", "new_password2")
def account(request: HttpRequest) -> HttpResponse:
    """Account (mockup 8): the customer's name and mobile, and their password. Email and
    company are shown, never posted. Two forms on one page, told apart by `action`; each
    re-renders with what was typed and the errors beside the fields, or redirects with a
    toast. Changing the password ends every session, so this one is signed in again."""
    customer = request.customer
    contact = customer.contact
    on_file = phone_display(contact.phone) if contact.phone else ""
    details = DetailsForm(initial={"name": contact.name, "mobile": on_file}, on_file=on_file)
    password = ChangePasswordForm(customer)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "details":
            details = DetailsForm(request.POST, on_file=on_file)
            if details.is_valid():
                try:
                    services.update_details(customer, **details.cleaned_data)
                except services.CustomerError as error:
                    details.add_error(error.field, str(error))
                else:
                    messages.success(request, "Details saved.")
                    return redirect("customers:account")
        elif action == "password":
            password = ChangePasswordForm(customer, request.POST)
            if password.is_valid():
                try:
                    services.change_password(
                        customer,
                        password.cleaned_data["current_password"],
                        password.cleaned_data["new_password1"],
                    )
                except services.CustomerError as error:
                    # Under the field it is about, or above the form when it names none.
                    password.add_error(error.field, str(error))
                else:
                    sign_in(request, customer)
                    messages.success(request, "Password changed.")
                    return redirect("customers:account")
        else:
            return HttpResponseBadRequest()
    return _page(
        request,
        "customers/account.html",
        {
            "details": details,
            "password": password,
            "email": customer.email,
            "company": contact.company.name if contact.company else "",
        },
    )
