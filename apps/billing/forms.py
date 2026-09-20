"""Field-level validation for the billing modals.

These forms are never rendered — the modals are hand-written to the approved mockup, the
same way the reservation editor is. They exist for what a form is genuinely better at than
a hand-rolled check: required fields, a real email validator, and errors keyed by field so
the modal can mark the offending input instead of showing one sentence at the top.

The rules above a single field — one account per contact, no two groups with one name, an
account never left without a group — belong to `services.py` and are not repeated here.
"""

from __future__ import annotations

from django import forms

from .models import Terms


class AccountForm(forms.Form):
    """The account itself: what QuickBooks will call this customer, and on what terms."""

    name = forms.CharField(max_length=200, error_messages={"required": "Give the account a name."})
    terms = forms.ChoiceField(choices=Terms.choices, initial=Terms.NET_30)


class NewAccountForm(AccountForm):
    """Creating one. The first group is part of it — an account with nothing to invoice
    against is not a state the office should be able to reach."""

    group_name = forms.CharField(
        max_length=200, error_messages={"required": "Name the first account group."}
    )
    invoice_email = forms.EmailField(required=False)
    po_required = forms.BooleanField(required=False)


class GroupForm(forms.Form):
    """A group: who an invoice is made out to. Blank terms means "whatever the account says",
    which is why `terms` allows the empty choice and `AccountForm.terms` does not."""

    name = forms.CharField(max_length=200, error_messages={"required": "Give the group a name."})
    invoice_email = forms.EmailField(required=False)
    terms = forms.ChoiceField(choices=[("", "")] + list(Terms.choices), required=False)
    po_required = forms.BooleanField(required=False)
