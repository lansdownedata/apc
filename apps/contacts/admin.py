from django.contrib import admin

from .models import Contact, ContactPhone


class ContactPhoneInline(admin.TabularInline):
    model = ContactPhone
    extra = 0
    fields = ("e164", "label", "is_primary", "opted_out")


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "channel", "phone", "email", "la_account_id")
    list_filter = ("channel",)
    # `phone` is a property (backed by ContactPhone), not a concrete field — search
    # the related table's e164 column instead, or this 500s (FieldError) on any query.
    search_fields = ("name", "company", "email", "phones__e164")
    inlines = (ContactPhoneInline,)
