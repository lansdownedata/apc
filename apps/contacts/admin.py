from django.contrib import admin

from .models import Company, Contact


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "billing_contact")
    search_fields = ("name",)


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "channel", "phone", "email", "la_account_id")
    list_filter = ("channel",)
    search_fields = ("name", "company", "email", "phone")
