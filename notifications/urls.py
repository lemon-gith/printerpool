from django.urls import path

from . import views

urlpatterns = [
    path("", views.account, name="account"),
    path("email/add/", views.email_add, name="email_add"),
    path("email/<int:pk>/verify/", views.email_verify, name="email_verify"),
    path("email/<int:pk>/resend/", views.email_resend, name="email_resend"),
    path("email/<int:pk>/preferences/", views.email_preferences, name="email_preferences"),
    path("email/<int:pk>/remove/", views.email_remove, name="email_remove"),
]
