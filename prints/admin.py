from django.contrib import admin

from .models import Print, Printer, PrintRequest, RequestFile, StatusEvent


class RequestFileInline(admin.TabularInline):
    model = RequestFile
    extra = 0
    readonly_fields = ("original_name", "size", "sha256", "file", "uploaded_at")
    can_delete = False


class PrintInline(admin.TabularInline):
    model = Print
    extra = 0
    fields = ("printer", "status", "priority", "est_duration_minutes", "est_filament_g", "filament_used_g", "started_at", "finished_at")


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
    readonly_fields = ("id", "created_at", "updated_at", "files_purged_at")
    inlines = [RequestFileInline, PrintInline, StatusEventInline]


@admin.register(Print)
class PrintAdmin(admin.ModelAdmin):
    list_display = ("id", "request", "printer", "status", "priority", "started_at", "finished_at", "filament_used_g")
    list_filter = ("status", "printer")
    search_fields = ("request__title", "request__user__username")


@admin.register(Printer)
class PrinterAdmin(admin.ModelAdmin):
    list_display = ("name", "model", "active")


@admin.register(StatusEvent)
class StatusEventAdmin(admin.ModelAdmin):
    list_display = ("at", "request", "print_job", "from_status", "to_status", "actor")
    list_filter = ("to_status",)
    search_fields = ("request__title", "actor__username")
