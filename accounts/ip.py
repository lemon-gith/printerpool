from django.conf import settings


def client_ip(request):
    """The visitor's real IP address, used for login throttling.

    Behind our own reverse proxy (Caddy), REMOTE_ADDR is the proxy's address, so every visitor would look
    identical. The proxy records the real address as the right-most X-Forwarded-For entry; anything to the
    left of that came from the client and can be forged, so we count in from the right by the number of
    proxies we actually run. With no proxy configured the header is ignored entirely.
    """
    remote_addr = request.META.get("REMOTE_ADDR", "")
    proxy_count = settings.TRUSTED_PROXY_COUNT
    if proxy_count <= 0:
        return remote_addr
    forwarded = [ip.strip() for ip in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if ip.strip()]
    if len(forwarded) >= proxy_count:
        return forwarded[-proxy_count]
    return remote_addr
