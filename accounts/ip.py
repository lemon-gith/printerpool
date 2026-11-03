from django.conf import settings


def client_ip(request):
    """The visitor's real IP address, used for login throttling.

    Behind a proxy, REMOTE_ADDR is the proxy's address, so every visitor would look identical:
    - With CLIENT_IP_HEADER set (e.g. Cloudflare's CF-Connecting-IP), that header holds the address. If it's
      missing we fall back to REMOTE_ADDR, never to X-Forwarded-For, so a client can't supply its own.
    - Otherwise, behind our own proxy (the gateway), it records the real address as the right-most
      X-Forwarded-For entry; anything to the left of that came from the client and can be forged, so we count
      in from the right by the number of proxies we actually run.
    With neither configured, headers are ignored entirely.
    """
    remote_addr = request.META.get("REMOTE_ADDR", "")
    if settings.CLIENT_IP_HEADER:
        return request.headers.get(settings.CLIENT_IP_HEADER, "").strip() or remote_addr
    proxy_count = settings.TRUSTED_PROXY_COUNT
    if proxy_count <= 0:
        return remote_addr
    forwarded = [ip.strip() for ip in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if ip.strip()]
    if len(forwarded) >= proxy_count:
        return forwarded[-proxy_count]
    return remote_addr
