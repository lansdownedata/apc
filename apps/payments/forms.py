"""Forms for the payments screens."""

from django import forms

from .models import AffiliatePayable


class PayableInvoiceForm(forms.ModelForm):
    """Only the invoice file: the pop-up posts the number and amount itself, and this gives
    `components/file_upload.html` a real field to render (never a bare file input)."""

    class Meta:
        model = AffiliatePayable
        fields = ["invoice_file"]
        labels = {"invoice_file": "Invoice"}
        widgets = {
            "invoice_file": forms.FileInput(
                attrs={"class": "sr-only", "accept": "application/pdf,image/*"}
            )
        }
