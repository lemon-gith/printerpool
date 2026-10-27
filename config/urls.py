from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from accounts.views import PasswordChangeView

admin.site.site_header = "PrintPool admin"
admin.site.site_title = "PrintPool admin"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("password/", PasswordChangeView.as_view(), name="password_change"),
    path("", include("prints.urls")),
]
