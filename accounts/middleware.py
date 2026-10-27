from django.shortcuts import redirect
from django.urls import reverse


class ForcePasswordChangeMiddleware:
    """Send users with a generated initial password to the change-password page until they pick their own."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and user.must_change_password:
            allowed = {reverse("password_change"), reverse("logout")}
            if request.path not in allowed and not request.path.startswith("/static/"):
                return redirect("password_change")
        return self.get_response(request)
