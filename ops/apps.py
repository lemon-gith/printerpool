from django.apps import AppConfig


class OpsConfig(AppConfig):
    """Operational jobs: backups, restores and the background worker."""

    name = "ops"
