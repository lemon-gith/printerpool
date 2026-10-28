import logging
import os

from django.core.files.storage import default_storage
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache

log = logging.getLogger(__name__)


@never_cache
def health(request):
    """Liveness check for Docker and uptime monitors: can we reach the database and write uploads?
    Deliberately says nothing beyond ok/failing, since it's public."""
    checks = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        checks["database"] = "ok"
    except Exception:
        log.exception("Health check: database unreachable")
        checks["database"] = "failing"
    location = getattr(default_storage, "location", None)  # local disk only; skipped for cloud storage
    if location is not None:
        checks["storage"] = "ok" if os.access(location, os.W_OK) else "failing"
    healthy = all(v == "ok" for v in checks.values())
    return JsonResponse({"status": "ok" if healthy else "failing", **checks}, status=200 if healthy else 503)
