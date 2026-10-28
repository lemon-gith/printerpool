import csv
import io

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.urls import path
from django.utils.cache import add_never_cache_headers

from . import bulk
from .models import User


class UserCreationForm(forms.ModelForm):
    """Admins only pick a username and role; the password is generated and shown once."""

    class Meta:
        model = User
        fields = ("username", "first_name", "last_name", "email", "role")


class ImportUsersForm(forms.Form):
    csv_file = forms.FileField(label="CSV file")


def password_sheet(request, issued, title):
    """One-time page listing new passwords as printable slips, plus a CSV download. Never cached."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["username", "first_name", "last_name", "role", "temporary_password"])
    for item in issued:
        u = item.user
        writer.writerow([u.username, u.first_name, u.last_name, u.role, item.password])
    response = render(
        request,
        "admin/accounts/user/password_sheet.html",
        {"title": title, "issued": issued, "csv_text": buffer.getvalue(), "site_url": settings.SITE_URL},
    )
    add_never_cache_headers(response)
    return response


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    add_form = UserCreationForm
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("username", "first_name", "last_name", "email", "role")}),
    )
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name", "email")}),
        ("Access", {"fields": ("role", "is_active", "must_change_password")}),
        ("Printing", {"fields": ("print_cap_grams_override",)}),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    readonly_fields = ("last_login", "date_joined")
    list_display = ("username", "first_name", "last_name", "role", "is_active", "must_change_password", "last_login")
    list_filter = ("role", "is_active", "must_change_password")
    actions = ["reset_password", "deactivate", "reactivate"]

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if "is_active" in form.base_fields:
            form.base_fields["is_active"].help_text = (
                "Untick to deactivate: the user is logged out and can't log in, but their requests and "
                "print history are kept. Users are never deleted."
            )
        return form

    change_list_template = "admin/accounts/user/change_list.html"

    def get_urls(self):
        return [
            path("import/", self.admin_site.admin_view(self.import_view), name="accounts_user_import"),
        ] + super().get_urls()

    def import_view(self, request):
        if not self.has_add_permission(request):
            raise PermissionDenied
        errors = []
        if request.method == "POST":
            form = ImportUsersForm(request.POST, request.FILES)
            if form.is_valid():
                rows, errors = bulk.parse_csv(form.cleaned_data["csv_file"])
                if not errors:
                    issued = bulk.create_accounts(rows)
                    for item in issued:
                        self.log_addition(request, item.user, [{"added": {}}])
                    return password_sheet(request, issued, f"{len(issued)} account(s) created")
        else:
            form = ImportUsersForm()
        context = {
            **self.admin_site.each_context(request),
            "title": "Import users from CSV",
            "form": form,
            "errors": errors,
            "opts": self.model._meta,
            "max_rows": bulk.MAX_ROWS,
        }
        return render(request, "admin/accounts/user/import.html", context)

    def save_model(self, request, obj, form, change):
        if change:
            super().save_model(request, obj, form, change)
        else:
            obj._issued = bulk.issue_password(obj)

    def response_add(self, request, obj, post_url_continue=None):
        # Show the new password on a one-time sheet instead of the usual "added" message.
        return password_sheet(request, [obj._issued], f"Account “{obj.username}” created")

    # Accounts are deactivated, never deleted, so print history and usage figures stay intact.
    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Reset password (generate new one-time password)")
    def reset_password(self, request, queryset):
        issued = [bulk.issue_password(user) for user in queryset.order_by("username")]
        return password_sheet(request, issued, f"New password(s) for {len(issued)} account(s)")

    @admin.action(description="Deactivate selected users")
    def deactivate(self, request, queryset):
        count = queryset.exclude(pk=request.user.pk).update(is_active=False)
        if queryset.filter(pk=request.user.pk).exists():
            messages.error(request, "You can't deactivate your own account.")
        messages.success(request, f"Deactivated {count} user(s).")

    @admin.action(description="Reactivate selected users")
    def reactivate(self, request, queryset):
        count = queryset.update(is_active=True)
        messages.success(request, f"Reactivated {count} user(s).")
