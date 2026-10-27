"""Workflow rules. All status changes go through here so permissions, expiry and the audit log stay consistent."""

import hashlib
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import Print, PrintRequest, RequestFile, StatusEvent

RS = PrintRequest.Status
PS = Print.Status

# --- File expiry -----------------------------------------------------------------------------
EXPIRY_ON_SUBMIT = timedelta(days=30)  # untouched requests
EXPIRY_AFTER_EVENT = timedelta(days=7)  # after rejection / recall / a print being set off

# Request statuses that end the request early: they (re)start the 7-day file clock and cancel any queued
# prints. Not allowed while something is physically printing; staff finish or cancel that print first.
REQUEST_CLOSING_STATUSES = {RS.REJECTED, RS.RECALLED}
# Print statuses that (re)start the 7-day clock when entered. Failed is included so that a
# reprint after a failure still has its files.
PRINT_STATUSES_SETTING_EXPIRY = {PS.PRINTING, PS.FAILED}

# --- Allowed transitions: {from: {to: who}} ----------------------------------------------------
# Vocabulary: owners *recall* requests, staff *reject* them, and *cancel* applies only to prints.
STAFF = "staff"
OWNER = "owner"

# approved → in_progress happens automatically when the first print starts (see transition_print).
REQUEST_TRANSITIONS = {
    RS.SUBMITTED: {RS.APPROVED: STAFF, RS.REJECTED: STAFF, RS.RECALLED: OWNER},
    RS.APPROVED: {RS.REJECTED: STAFF, RS.RECALLED: OWNER},
    RS.IN_PROGRESS: {RS.COMPLETED: STAFF, RS.REJECTED: STAFF, RS.RECALLED: OWNER},
}

# Ordered: this is also the button order in the UI.
PRINT_TRANSITIONS = {
    PS.QUEUED: (PS.PRINTING, PS.CANCELLED),
    PS.PRINTING: (PS.SUCCEEDED, PS.FAILED, PS.CANCELLED),
}


def can_view_request(user, req):
    return user.is_authenticated and (user.is_print_staff or req.user_id == user.pk)


def request_blocked_reason(req):
    """Why the request can't be closed or completed right now, or None."""
    if req.prints.filter(status=PS.PRINTING).exists():
        return "A print is running. Staff need to finish or cancel it first."
    return None


def allowed_request_transitions(user, req):
    is_owner = req.user_id == user.pk
    options = REQUEST_TRANSITIONS.get(req.status, {})
    allowed = [s for s, who in options.items() if (who == STAFF and user.is_print_staff) or (who == OWNER and is_owner)]
    if not allowed:
        return allowed
    if request_blocked_reason(req):
        allowed = [s for s in allowed if s not in REQUEST_CLOSING_STATUSES and s != RS.COMPLETED]
    if RS.COMPLETED in allowed and req.prints.filter(status=PS.QUEUED).exists():
        allowed.remove(RS.COMPLETED)  # finish or cancel the queued prints first
    return allowed


def _bump_expiry(req, now):
    if req.files_purged_at is None:
        req.files_expire_at = now + EXPIRY_AFTER_EVENT


@transaction.atomic
def transition_request(req, new_status, actor, note=""):
    req = PrintRequest.objects.select_for_update().get(pk=req.pk)
    if new_status not in allowed_request_transitions(actor, req):
        raise PermissionDenied(request_blocked_reason(req) or f"Cannot move request from {req.status} to {new_status}.")

    now = timezone.now()
    old_status = req.status
    req.status = new_status
    if new_status in (RS.APPROVED, RS.REJECTED) and actor.is_print_staff:
        req.reviewed_by = actor
        req.reviewed_at = now
        req.review_note = note
    if new_status in REQUEST_CLOSING_STATUSES:
        _bump_expiry(req, now)
    req.save()

    StatusEvent.objects.create(request=req, from_status=old_status, to_status=new_status, actor=actor, note=note)

    if new_status in REQUEST_CLOSING_STATUSES:
        for job in req.prints.select_for_update().filter(status=PS.QUEUED):
            job.status = PS.CANCELLED
            job.finished_at = now
            job.save(update_fields=["status", "finished_at"])
            StatusEvent.objects.create(
                request=req, print_job=job, from_status=PS.QUEUED, to_status=PS.CANCELLED, actor=actor,
                note=f"Request {RS(new_status).label.lower()}",
            )
    return req


@transaction.atomic
def create_print(req, actor, *, printer, est_duration_minutes, est_filament_g, priority=0, notes=""):
    if not actor.is_print_staff:
        raise PermissionDenied
    req = PrintRequest.objects.select_for_update().get(pk=req.pk)
    if req.status not in (RS.APPROVED, RS.IN_PROGRESS):
        raise ValidationError("Prints can only be queued for approved or in-progress requests.")
    job = Print.objects.create(
        request=req,
        printer=printer,
        est_duration_minutes=est_duration_minutes,
        est_filament_g=est_filament_g,
        priority=priority,
        notes=notes,
        created_by=actor,
    )
    StatusEvent.objects.create(request=req, print_job=job, to_status=PS.QUEUED, actor=actor, note=notes)
    return job


@transaction.atomic
def transition_print(job, new_status, actor, *, filament_used_g=None, note=""):
    if not actor.is_print_staff:
        raise PermissionDenied
    job = Print.objects.select_for_update().get(pk=job.pk)
    req = PrintRequest.objects.select_for_update().get(pk=job.request_id)
    if new_status not in PRINT_TRANSITIONS.get(job.status, set()):
        raise ValidationError(f"Cannot move print from {job.status} to {new_status}.")

    now = timezone.now()
    old_status = job.status
    job.status = new_status
    if new_status == PS.PRINTING:
        job.started_at = now
    if new_status in (PS.SUCCEEDED, PS.FAILED, PS.CANCELLED):
        job.finished_at = now
        if filament_used_g is not None:
            job.filament_used_g = filament_used_g
        elif new_status == PS.SUCCEEDED:
            job.filament_used_g = job.est_filament_g
    job.save()
    StatusEvent.objects.create(
        request=req, print_job=job, from_status=old_status, to_status=new_status, actor=actor, note=note
    )

    if new_status in PRINT_STATUSES_SETTING_EXPIRY:
        _bump_expiry(req, now)
    # The first print being set off moves the request along automatically.
    if new_status == PS.PRINTING and req.status == RS.APPROVED:
        req.status = RS.IN_PROGRESS
        StatusEvent.objects.create(
            request=req, from_status=RS.APPROVED, to_status=RS.IN_PROGRESS, actor=actor, note="First print started"
        )
    req.save()
    return job


# --- Uploads --------------------------------------------------------------------------------
def save_uploaded_files(req, uploaded_files):
    for upload in uploaded_files:
        digest = hashlib.sha256()
        for chunk in upload.chunks():
            digest.update(chunk)
        upload.seek(0)
        rf = RequestFile(request=req, original_name=upload.name[:255], size=upload.size, sha256=digest.hexdigest())
        rf.file.save(upload.name, upload, save=False)
        rf.save()


def recall_source_for(user, pk):
    """The recalled request `user` may resubmit from, or None if there isn't a valid one."""
    return (
        PrintRequest.objects.filter(pk=pk, user=user, status=RS.RECALLED, replaced_by__isnull=True)
        .prefetch_related("files")
        .first()
    )


@transaction.atomic
def submit_request(user, *, files, supersedes=None, **fields):
    """Create a request. When resubmitting a recalled one (`supersedes`) without uploading anything, the new
    request takes over the original's files: the same stored files, now expiring on the new request's
    schedule. Uploading anything replaces them instead, and the originals stay with the recalled request."""
    take_over_files = False
    if supersedes is not None:
        # Lock the original so the same recalled request can't be resubmitted twice concurrently.
        locked = PrintRequest.objects.select_for_update().filter(pk=supersedes.pk).first()
        if locked is None or recall_source_for(user, supersedes.pk) is None:
            raise ValidationError("That request has already been resubmitted or can't be edited.")
        if not files:
            if not (locked.files_available and locked.files.exists()):
                raise ValidationError("The original files have expired; please upload them again.")
            take_over_files = True
    elif not files:
        raise ValidationError("A request needs at least one file.")

    req = PrintRequest.objects.create(user=user, supersedes=supersedes, **fields)
    if take_over_files:
        supersedes.files.update(request=req)
    save_uploaded_files(req, files)

    note = ""
    if supersedes is not None:
        how = "files carried over" if take_over_files else "new files uploaded"
        note = f"Replaces recalled request “{supersedes.title}” ({how})"
    StatusEvent.objects.create(request=req, to_status=RS.SUBMITTED, actor=user, note=note)
    return req


# --- Expiry cleanup ---------------------------------------------------------------------------
def purge_expired_files(now=None, dry_run=False):
    """Delete stored files for requests past their expiry. Metadata rows are kept for history."""
    now = now or timezone.now()
    expired = PrintRequest.objects.filter(files_expire_at__lte=now, files_purged_at__isnull=True)
    purged = []
    for req in expired.prefetch_related("files"):
        if not dry_run:
            for rf in req.files.all():
                # Deleting a file that's already gone is a no-op, so a crashed run can simply be re-run.
                rf.file.storage.delete(rf.file.name)
            req.files_purged_at = now
            req.save(update_fields=["files_purged_at"])
        purged.append(req)
    return purged


# --- Print cap ------------------------------------------------------------------------------
# Filament counts towards the cap for prints that consumed material.
CAP_COUNTED_STATUSES = (PS.SUCCEEDED, PS.FAILED)


def filament_used(user, start, end):
    total = Print.objects.filter(
        request__user=user,
        status__in=CAP_COUNTED_STATUSES,
        finished_at__gte=start,
        finished_at__lt=end,
    ).aggregate(total=Sum("filament_used_g"))["total"]
    return total or 0


def month_bounds(now=None):
    now = timezone.localtime(now or timezone.now())
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


def cap_status(user, now=None):
    start, end = month_bounds(now)
    cap = user.print_cap_grams_override
    if cap is None:
        cap = settings.PRINT_CAP_GRAMS_PER_MONTH
    used = filament_used(user, start, end)
    return {"used_g": used, "cap_g": cap, "remaining_g": max(cap - used, 0), "period_start": start, "over": used >= cap}
