from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.utils.html import format_html

from .models import User, generate_initial_password


class UserCreationForm(forms.ModelForm):
    """Admins only pick a username and role; the password is generated and shown once."""

    class Meta:
        model = User
        fields = ("username", "first_name", "last_name", "email", "role")


def _issue_password(request, user):
    password = generate_initial_password()
    user.set_password(password)
    user.must_change_password = True
    user.save()
    messages.warning(
        request,
        format_html(
            "Initial password for <strong>{}</strong>: <code style='font-size:1.2em'>{}</code> "
            "&mdash; copy it now, it will not be shown again.",
            user.username,
            password,
        ),
    )


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

    def save_model(self, request, obj, form, change):
        if change:
            super().save_model(request, obj, form, change)
        else:
            _issue_password(request, obj)

    # Accounts are deactivated, never deleted, so print history and usage figures stay intact.
    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Reset password (generate new one-time password)")
    def reset_password(self, request, queryset):
        for user in queryset:
            _issue_password(request, user)

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
