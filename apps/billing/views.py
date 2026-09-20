"""Billing-account endpoints for the customer profile.

Every one is POST-only, gated on payments access, and answers JSON: the modals are Alpine,
and a redirect on a validation error would throw away everything typed. Two shapes come
back on a refusal, and the modal reads both:

* `{"errors": {"<field>": "<message>"}}` — a field is wrong, mark that input.
* `{"error": "<sentence>"}` — a billing rule refused the change as a whole.

Nothing here touches a model. `services.py` owns the rules, so the order page and the
QuickBooks sync land on the same ones.
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from apps.accounts.permissions import payment_access_required
from apps.contacts.models import Contact

from . import services
from .forms import AccountForm, GroupForm, NewAccountForm
from .models import AccountGroup, BillingAccount


def _invalid(form) -> JsonResponse:
    """Field errors, first message per field — the modal shows one line under each input."""
    return JsonResponse(
        {"ok": False, "errors": {name: errs[0] for name, errs in form.errors.items()}},
        status=400,
    )


def _refused(exc: services.BillingError) -> JsonResponse:
    return JsonResponse({"ok": False, "error": str(exc)}, status=400)


@login_required
@payment_access_required
@require_POST
def account_create(request: HttpRequest, contact_pk: int) -> JsonResponse:
    contact = get_object_or_404(Contact, pk=contact_pk)
    form = NewAccountForm(request.POST)
    if not form.is_valid():
        return _invalid(form)
    data = form.cleaned_data
    try:
        account = services.create_account(
            contact,
            name=data["name"],
            terms=data["terms"],
            group_name=data["group_name"],
            invoice_email=data["invoice_email"],
            po_required=data["po_required"],
        )
    except services.BillingError as exc:
        return _refused(exc)
    return JsonResponse({"ok": True, "account": account.pk})


@login_required
@payment_access_required
@require_POST
def account_update(request: HttpRequest, pk: int) -> JsonResponse:
    account = get_object_or_404(BillingAccount, pk=pk)
    form = AccountForm(request.POST)
    if not form.is_valid():
        return _invalid(form)
    try:
        services.update_account(
            account, name=form.cleaned_data["name"], terms=form.cleaned_data["terms"]
        )
    except services.BillingError as exc:
        return _refused(exc)
    return JsonResponse({"ok": True})


@login_required
@payment_access_required
@require_POST
def group_add(request: HttpRequest, pk: int) -> JsonResponse:
    account = get_object_or_404(BillingAccount, pk=pk)
    form = GroupForm(request.POST)
    if not form.is_valid():
        return _invalid(form)
    data = form.cleaned_data
    try:
        group = services.add_group(
            account,
            name=data["name"],
            invoice_email=data["invoice_email"],
            terms=data["terms"],
            po_required=data["po_required"],
        )
    except services.BillingError as exc:
        return _refused(exc)
    return JsonResponse({"ok": True, "group": group.pk})


@login_required
@payment_access_required
@require_POST
def group_update(request: HttpRequest, pk: int) -> JsonResponse:
    group = get_object_or_404(AccountGroup.objects.select_related("account"), pk=pk)
    form = GroupForm(request.POST)
    if not form.is_valid():
        return _invalid(form)
    data = form.cleaned_data
    try:
        services.update_group(
            group,
            name=data["name"],
            invoice_email=data["invoice_email"],
            terms=data["terms"],
            po_required=data["po_required"],
        )
    except services.BillingError as exc:
        return _refused(exc)
    return JsonResponse({"ok": True})


@login_required
@payment_access_required
@require_POST
def group_default(request: HttpRequest, pk: int) -> JsonResponse:
    group = get_object_or_404(AccountGroup, pk=pk)
    services.set_default_group(group)
    return JsonResponse({"ok": True})


@login_required
@payment_access_required
@require_POST
def group_delete(request: HttpRequest, pk: int) -> JsonResponse:
    group = get_object_or_404(AccountGroup, pk=pk)
    try:
        services.delete_group(group)
    except services.BillingError as exc:
        return _refused(exc)
    return JsonResponse({"ok": True})
