from django.contrib import admin

from .models import EmailAddress


@admin.register(EmailAddress)
class EmailAddressAdmin(admin.ModelAdmin):
    list_display = ("email", "user", "verified_at", "notify_events", "created_at")
    list_filter = ("verified_at",)
    search_fields = ("email", "user__username")
    # Addresses are managed by their owners (verification, opt-ins); admins can only view or remove them.
    fields = ("user", "email", "verified_at", "notify_events", "created_at")
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
