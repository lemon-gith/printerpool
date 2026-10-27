import uuid
from datetime import timedelta
from pathlib import PurePath

from django.conf import settings
from django.db import models
from django.utils import timezone


def model_upload_path(instance, filename):
    # Never trust the user's filename for the path; it's kept separately in `original_name`.
    ext = PurePath(filename).suffix.lower()
    return f"models/{instance.id}{ext}"


class PrintRequest(models.Model):
    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETED = "completed", "Completed"
        # Withdrawn by its owner. They can edit and resubmit it as a new request (see `supersedes`), or leave it.
        RECALLED = "recalled", "Recalled"

    class Material(models.TextChoices):
        PLA = "pla", "PLA"
        PETG = "petg", "PETG"
        TPU = "tpu", "TPU"
        OTHER = "other", "Other (see notes)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="print_requests")
    title = models.CharField(max_length=200)
    notes = models.TextField(blank=True)
    material = models.CharField(max_length=10, choices=Material.choices, default=Material.PLA)
    colour = models.CharField(max_length=50, blank=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SUBMITTED, db_index=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)

    files_expire_at = models.DateTimeField(db_index=True)
    files_purged_at = models.DateTimeField(null=True, blank=True)

    # Requests are immutable; editing one means recalling it and submitting a replacement.
    supersedes = models.OneToOneField(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="replaced_by"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if self.files_expire_at is None:
            from .services import EXPIRY_ON_SUBMIT

            self.files_expire_at = timezone.now() + EXPIRY_ON_SUBMIT
        super().save(*args, **kwargs)

    @property
    def files_available(self):
        return self.files_purged_at is None and self.files_expire_at > timezone.now()


class RequestFile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(PrintRequest, on_delete=models.CASCADE, related_name="files")
    file = models.FileField(upload_to=model_upload_path, max_length=255)
    original_name = models.CharField(max_length=255)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["original_name"]

    def __str__(self):
        return self.original_name


class Printer(models.Model):
    name = models.CharField(max_length=100, unique=True)
    model = models.CharField(max_length=100, blank=True)
    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Print(models.Model):
    """One physical print attempt. A request can have several (reprints, multi-part jobs)."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        PRINTING = "printing", "Printing"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    request = models.ForeignKey(PrintRequest, on_delete=models.CASCADE, related_name="prints")
    printer = models.ForeignKey(Printer, on_delete=models.SET_NULL, null=True, blank=True, related_name="prints")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED, db_index=True)
    # Higher runs first. Unused for now; ready for scheduling later.
    priority = models.IntegerField(default=0)

    est_duration_minutes = models.PositiveIntegerField(help_text="From the slicer.")
    est_filament_g = models.PositiveIntegerField(help_text="From the slicer.")
    filament_used_g = models.PositiveIntegerField(
        null=True, blank=True, help_text="Actual usage, recorded when the print finishes. Counts towards the cap."
    )

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True, db_index=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-priority", "created_at"]

    def __str__(self):
        return f"Print #{self.pk} of {self.request}"

    @property
    def est_finish_at(self):
        if self.started_at:
            return self.started_at + timedelta(minutes=self.est_duration_minutes)
        return None

    @property
    def est_remaining(self):
        if self.status != self.Status.PRINTING or not self.started_at:
            return None
        return max(self.est_finish_at - timezone.now(), timedelta(0))

    @property
    def actual_duration(self):
        if self.started_at and self.finished_at:
            return self.finished_at - self.started_at
        return None


class StatusEvent(models.Model):
    """Audit trail: every status change on a request or one of its prints."""

    request = models.ForeignKey(PrintRequest, on_delete=models.CASCADE, related_name="events")
    print_job = models.ForeignKey(Print, on_delete=models.CASCADE, null=True, blank=True, related_name="events")
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    note = models.TextField(blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["at", "id"]

    def __str__(self):
        target = f"print #{self.print_job_id}" if self.print_job_id else "request"
        return f"{target}: {self.from_status or '∅'} → {self.to_status}"
