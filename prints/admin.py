from django.contrib import admin

from .models import Print, Printer, PrintRequest, RequestFile, StatusEvent


class RequestFileInline(admin.TabularInline):
    model = RequestFile
    extra = 0
    readonly_fields = ("original_name", "size", "sha256", "file", "uploaded_at")
    can_delete = False


# Statuses (and the timestamps that go with them) only change through the workflow in services.py, so
# that history and file expiry stay consistent. Admins can still correct estimates, filament used,
# printer, priority and expiry dates.
PRINT_WORKFLOW_FIELDS = ("status", "started_at", "finished_at")


class PrintInline(admin.TabularInline):
    model = Print
    extra = 0
    fields = ("printer", "status", "priority", "est_duration_minutes", "est_filament_g", "filament_used_g", "started_at", "finished_at")
    readonly_fields = PRINT_WORKFLOW_FIELDS

    def has_add_permission(self, request, obj=None):
        return False  # prints are queued from the request page so they get logged


class StatusEventInline(admin.TabularInline):
    model = StatusEvent
    extra = 0
    readonly_fields = ("at", "print_job", "from_status", "to_status", "actor", "note")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(PrintRequest)
class PrintRequestAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "status", "material", "created_at", "files_expire_at", "files_purged_at")
    list_filter = ("status", "material")
    search_fields = ("title", "user__username", "notes")
    date_hierarchy = "created_at"
    readonly_fields = ("id", "user", "status", "reviewed_by", "reviewed_at", "created_at", "updated_at", "files_purged_at")
    inlines = [RequestFileInline, PrintInline, StatusEventInline]

    def has_add_permission(self, request):
        return False  # requests are submitted through the site so files get validated


@admin.register(Print)
class PrintAdmin(admin.ModelAdmin):
    list_display = ("id", "request", "printer", "status", "priority", "started_at", "finished_at", "filament_used_g")
    list_filter = ("status", "printer")
    search_fields = ("request__title", "request__user__username")
    readonly_fields = ("request", "created_by", "created_at") + PRINT_WORKFLOW_FIELDS

    def has_add_permission(self, request):
        return False


@admin.register(Printer)
class PrinterAdmin(admin.ModelAdmin):
    list_display = ("name", "model", "active")


@admin.register(StatusEvent)
class StatusEventAdmin(admin.ModelAdmin):
    list_display = ("at", "request", "print_job", "from_status", "to_status", "actor")
    list_filter = ("to_status",)
    search_fields = ("request__title", "actor__username")

    # The audit log is append-only.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
