from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import services
from .forms import PrintActionForm, PrintCreateForm, PrintRequestForm, RequestActionForm
from .models import Print, PrintRequest, RequestFile

REQUESTS_PER_PAGE = 25


def staff_required(view):
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_print_staff:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


def _get_visible_request(user, pk):
    req = get_object_or_404(PrintRequest.objects.select_related("user"), pk=pk)
    # 404 rather than 403 so users can't probe for other people's request IDs.
    if not services.can_view_request(user, req):
        raise Http404
    return req


@login_required
def dashboard(request):
    user = request.user
    qs = PrintRequest.objects.select_related("user")
    status_filter = request.GET.get("status", "")
    search = request.GET.get("q", "").strip()
    if status_filter in PrintRequest.Status.values:
        qs = qs.filter(status=status_filter)
    else:
        # A recalled request that has been resubmitted is just clutter next to its replacement;
        # it's still reachable from the replacement and via the "Recalled" filter.
        qs = qs.exclude(status=PrintRequest.Status.RECALLED, replaced_by__isnull=False)
    if user.is_print_staff:
        if search:
            qs = qs.filter(
                Q(title__icontains=search)
                | Q(user__username__icontains=search)
                | Q(user__first_name__icontains=search)
                | Q(user__last_name__icontains=search)
            )
        active_prints = Print.objects.filter(status__in=[Print.Status.QUEUED, Print.Status.PRINTING]).select_related(
            "request", "request__user", "printer"
        )
    else:
        qs = qs.filter(user=user)
        if search:
            qs = qs.filter(title__icontains=search)
        active_prints = None

    page = Paginator(qs, REQUESTS_PER_PAGE).get_page(request.GET.get("page"))
    # Keep the current filters when moving between pages.
    filters = request.GET.copy()
    filters.pop("page", None)
    return render(
        request,
        "prints/dashboard.html",
        {
            "page": page,
            "filter_query": filters.urlencode(),
            "active_prints": active_prints,
            "statuses": PrintRequest.Status.choices,
            "status_filter": status_filter,
            "search": search,
            "cap": services.cap_status(user),
        },
    )


@login_required
def request_create(request):
    # `?from=<id>` means "resubmit my recalled request": pre-fill from it and link the new request to it.
    source = None
    if source_id := request.GET.get("from"):
        try:
            source = services.recall_source_for(request.user, source_id)
        except ValidationError:  # malformed UUID
            source = None
        if source is None:
            messages.error(request, "That request can't be edited (it may already have been resubmitted).")
            return redirect("request_create")

    if request.method == "POST":
        form = PrintRequestForm(request.POST, request.FILES, source=source)
        if form.is_valid():
            data = dict(form.cleaned_data)
            files = data.pop("files")
            keep_files = data.pop("keep_files", [])
            try:
                req = services.submit_request(
                    request.user, files=files, supersedes=source, keep_files=keep_files, **data
                )
            except ValidationError as e:
                form.add_error(None, e)
            else:
                messages.success(request, "Updated request submitted." if source else "Request submitted.")
                return redirect("request_detail", pk=req.pk)
    else:
        form = PrintRequestForm(source=source)
    return render(
        request,
        "prints/request_form.html",
        {"form": form, "source": source, "cap": services.cap_status(request.user)},
    )


@login_required
def request_detail(request, pk):
    req = _get_visible_request(request.user, pk)
    prints = list(req.prints.select_related("printer").order_by("created_at"))
    is_staff = request.user.is_print_staff
    for p in prints:
        p.allowed_transitions = services.PRINT_TRANSITIONS.get(p.status, ()) if is_staff else ()
    ctx = {
        "req": req,
        "files": req.files.all(),
        "prints": prints,
        "events": req.events.select_related("actor", "print_job"),
        "transitions": [(s, PrintRequest.Status(s).label) for s in services.allowed_request_transitions(request.user, req)],
        "owner_cap": services.cap_status(req.user),
        "can_queue_print": is_staff and req.status in (PrintRequest.Status.APPROVED, PrintRequest.Status.IN_PROGRESS),
        "replaced_by": getattr(req, "replaced_by", None),
        "can_resubmit": (
            req.status == PrintRequest.Status.RECALLED
            and req.user_id == request.user.pk
            and not hasattr(req, "replaced_by")
        ),
        "print_form": PrintCreateForm() if is_staff else None,
    }
    return render(request, "prints/request_detail.html", ctx)


@login_required
@require_POST
def request_action(request, pk):
    req = _get_visible_request(request.user, pk)
    form = RequestActionForm(request.POST)
    if form.is_valid():
        to_status = form.cleaned_data["to_status"]
        try:
            services.transition_request(req, to_status, request.user, form.cleaned_data["note"])
        except (PermissionDenied, ValidationError) as e:
            messages.error(request, str(e))
        else:
            if to_status == PrintRequest.Status.RECALLED:
                messages.success(request, "Request recalled. Update the details below and resubmit.")
                return redirect(f"{reverse('request_create')}?from={req.pk}")
            messages.success(request, "Request updated.")
    return redirect("request_detail", pk=req.pk)


@staff_required
@require_POST
def print_create(request, pk):
    req = _get_visible_request(request.user, pk)
    form = PrintCreateForm(request.POST)
    if form.is_valid():
        try:
            services.create_print(req, request.user, **form.cleaned_data)
            messages.success(request, "Print queued.")
        except ValidationError as e:
            messages.error(request, " ".join(e.messages))
    else:
        messages.error(request, "Couldn't queue print: please check the estimates.")
    return redirect("request_detail", pk=req.pk)


@staff_required
@require_POST
def print_action(request, print_id):
    job = get_object_or_404(Print, pk=print_id)
    form = PrintActionForm(request.POST)
    if form.is_valid():
        try:
            services.transition_print(
                job,
                form.cleaned_data["to_status"],
                request.user,
                filament_used_g=form.cleaned_data["filament_used_g"],
                note=form.cleaned_data["note"],
            )
            messages.success(request, "Print updated.")
        except ValidationError as e:
            messages.error(request, " ".join(e.messages))
    return redirect("request_detail", pk=job.request_id)


@login_required
def file_download(request, file_id):
    rf = get_object_or_404(RequestFile.objects.select_related("request"), pk=file_id)
    if not services.can_view_request(request.user, rf.request):
        raise Http404
    # Check expiry here too, so files vanish at their deadline even if the purge job hasn't run yet.
    if not rf.request.files_available:
        raise Http404("This file has expired.")
    try:
        handle = rf.file.open("rb")
    except FileNotFoundError:
        raise Http404
    return FileResponse(handle, as_attachment=True, filename=rf.original_name)
