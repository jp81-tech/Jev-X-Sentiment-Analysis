"""Single-process local UI access policy; proxies always require a token."""
import secrets
from ipaddress import ip_address
from urllib.parse import urlsplit
from fastapi import HTTPException, Request
from app.core.config import settings


def require_host(request: Request):
    allowed = {h.strip().lower() for h in settings.ALLOWED_HOSTS.split(',') if h.strip()}
    try:
        host = urlsplit('http://' + request.headers.get('host', '')).hostname
    except ValueError:
        host = None
    if not host or host.lower() not in allowed:
        raise HTTPException(403, 'Forbidden host')


def require_access(request: Request):
    require_host(request)
    origin = request.headers.get('origin')
    if origin and origin not in {o.strip() for o in settings.ALLOWED_ORIGINS.split(',')}:
        raise HTTPException(403, 'Forbidden origin')
    if settings.ADMIN_TOKEN:
        supplied = request.headers.get('x-admin-token', '')
        if not secrets.compare_digest(supplied.encode(), settings.ADMIN_TOKEN.encode()):
            raise HTTPException(403, 'Access token required')
        return
    try:
        local = bool(request.client and ip_address(request.client.host).is_loopback)
    except ValueError:
        local = False
    proxy = any(k.lower() == 'forwarded' or k.lower().startswith('x-forwarded-') or k.lower() == 'x-real-ip'
                for k in request.headers)
    if not local or proxy:
        raise HTTPException(403, 'Direct loopback or configured access token required')
