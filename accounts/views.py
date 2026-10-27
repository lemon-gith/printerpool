from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.urls import reverse_lazy


class PasswordChangeView(auth_views.PasswordChangeView):
    success_url = reverse_lazy("dashboard")

    def form_valid(self, form):
        response = super().form_valid(form)
        user = form.user
        if user.must_change_password:
            user.must_change_password = False
            user.save(update_fields=["must_change_password"])
        messages.success(self.request, "Password changed.")
        return response
