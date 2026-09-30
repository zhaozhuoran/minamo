"""Authentication helpers and FastAPI dependencies for Minamo Admin Console.
"""
from __future__ import annotations

import hmac
import secrets
from typing import Optional
from fastapi import Header, HTTPException, Request, Depends

# Store generated session tokens in memory
_ADMIN_SESSIONS: set[str] = set()


def create_admin_session() -> str:
    token = secrets.token_hex(32)
    _ADMIN_SESSIONS.add(token)
    return token


def validate_admin_session(token: str) -> bool:
    return token in _ADMIN_SESSIONS


def authenticate_admin_credentials(
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    password: Optional[str] = None,
    settings: Optional[object] = None,
) -> bool:
    if not settings:
        return False

    # 1. Check custom password if configured in settings.admin.password
    admin_cfg = getattr(settings, "admin", None)
    admin_password = getattr(admin_cfg, "password", "") if admin_cfg else ""

    if admin_password and password:
        if hmac.compare_digest(password, admin_password):
            return True

    # 2. Check password against secret_key
    if password and hmac.compare_digest(password, getattr(settings, "secret_key", "")):
        return True

    # 3. Check access_key and secret_key pair
    expected_access = getattr(settings, "access_key", "")
    expected_secret = getattr(settings, "secret_key", "")

    if access_key and secret_key:
        if hmac.compare_digest(access_key, expected_access) and hmac.compare_digest(secret_key, expected_secret):
            return True

    return False


async def require_admin(
    request: Request,
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
    authorization: Optional[str] = Header(None),
    x_admin_access_key: Optional[str] = Header(None, alias="X-Admin-Access-Key"),
    x_admin_secret_key: Optional[str] = Header(None, alias="X-Admin-Secret-Key"),
) -> str:
    settings = getattr(request.app.state, "settings", None)

    # 1. Check Session Token
    token = x_admin_token
    if not token and authorization:
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()

    if token and validate_admin_session(token):
        return "admin"

    # 2. Check Key Headers
    if x_admin_access_key and x_admin_secret_key:
        if authenticate_admin_credentials(access_key=x_admin_access_key, secret_key=x_admin_secret_key, settings=settings):
            return "admin"

    # 3. Allow requests if enforce_signature is False and no token/password is set
    # (dev mode convenience)
    if settings and not settings.enforce_signature:
        if not x_admin_token and not authorization and not x_admin_access_key and not x_admin_secret_key:
            return "admin"

    raise HTTPException(status_code=401, detail="Unauthorized: Invalid Admin Credentials or Session Token")
