from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import services
from .forms import AddEmailForm, PreferencesForm, VerifyCodeForm
from .models import EmailAddress


def email_feature(view):
    """The email pages exist only when email is enabled on this server ([email] in printpool.toml)."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not settings.EMAIL_ENABLED:
            raise Http404
        return view(request, *args, **kwargs)

    return wrapper


def _own_address(request, pk):
    return get_object_or_404(EmailAddress, pk=pk, user=request.user)


def _report(request, error):
    for message in error.messages:
        messages.error(request, message)


@login_required
def account(request, add_form=None):
    if not settings.EMAIL_ENABLED:
        return render(request, "notifications/account.html", {"email_enabled": False})
    addresses = list(request.user.email_addresses.all())
    for address in addresses:
        if address.is_verified:
            address.prefs_form = PreferencesForm(address=address)
    return render(
        request,
        "notifications/account.html",
        {
            "addresses": addresses,
            "add_form": add_form or AddEmailForm(),
            "can_add": len(addresses) < services.MAX_ADDRESSES_PER_USER,
            "max_addresses": services.MAX_ADDRESSES_PER_USER,
            "email_enabled": True,
        },
    )


@login_required
@email_feature
@require_POST
def email_add(request):
    form = AddEmailForm(request.POST)
    if not form.is_valid():
        return account(request, add_form=form)
    try:
        address = services.add_address(request.user, form.cleaned_data["email"])
    except ValidationError as e:
        if request.user.email_addresses.filter(email=form.cleaned_data["email"].strip().lower()).exists():
            _report(request, e)  # added, but the code email failed; they can resend
            return redirect("account")
        form.add_error("email", e)
        return account(request, add_form=form)
    messages.success(request, f"We've sent a 6-digit code to {address.email}. Enter it below to confirm the address.")
    return redirect("account")


@login_required
@email_feature
@require_POST
def email_verify(request, pk):
    address = _own_address(request, pk)
    form = VerifyCodeForm(request.POST)
    if not form.is_valid():
        _report(request, ValidationError(form.errors["code"]))
        return redirect("account")
    try:
        services.verify_code(address, form.cleaned_data["code"])
    except ValidationError as e:
        _report(request, e)
    else:
        messages.success(request, f"{address.email} is verified. Choose below which notifications it should get.")
    return redirect("account")


@login_required
@email_feature
@require_POST
def email_resend(request, pk):
    address = _own_address(request, pk)
    try:
        services.send_code(address)
    except ValidationError as e:
        _report(request, e)
    else:
        messages.success(request, f"New code sent to {address.email}.")
    return redirect("account")


@login_required
@email_feature
@require_POST
def email_preferences(request, pk):
    address = _own_address(request, pk)
    form = PreferencesForm(request.POST, address=address)
    if form.is_valid():
        try:
            services.set_preferences(address, form.cleaned_data["events"])
        except ValidationError as e:
            _report(request, e)
        else:
            count = len(address.notify_events)
            messages.success(
                request,
                f"Saved. {address.email} will get {count} kind(s) of notification." if count
                else f"Saved. {address.email} won't get any notifications.",
            )
    return redirect("account")


@login_required
@email_feature
@require_POST
def email_remove(request, pk):
    address = _own_address(request, pk)
    address.delete()
    messages.success(request, f"Removed {address.email}.")
    return redirect("account")
