from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("requests/new/", views.request_create, name="request_create"),
    path("requests/<uuid:pk>/", views.request_detail, name="request_detail"),
    path("requests/<uuid:pk>/action/", views.request_action, name="request_action"),
    path("requests/<uuid:pk>/prints/", views.print_create, name="print_create"),
    path("prints/<int:print_id>/action/", views.print_action, name="print_action"),
    path("files/<uuid:file_id>/", views.file_download, name="file_download"),
]
