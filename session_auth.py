"""Signed session and role layer for the standalone LableLens deployment.

This module intentionally does not pretend to provide government identity verification.
Staff access is deployment-configured through environment variables so the surrounding
role/authorization model can later be replaced by OAuth/SSO without changing API policy.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional, Dict, Any, Iterable

SESSION_TTL_SECONDS = max(900, int(os.getenv("SESSION_TTL_SECONDS", str(24 * 60 * 60))))
_SECRET = os.getenv("LABLELENS_SECRET") or secrets.token_urlsafe(48)

ALLOW_DEMO_REVIEWER = os.getenv("ALLOW_DEMO_REVIEWER", "false").lower() in {"1", "true", "yes"}
DEMO_REVIEWER_CODE = os.getenv("DEMO_REVIEWER_CODE", "").strip()

ALLOW_DEMO_OFFICER = os.getenv("ALLOW_DEMO_OFFICER", "false").lower() in {"1", "true", "yes"} or ALLOW_DEMO_REVIEWER
DEMO_OFFICER_CODE = (os.getenv("DEMO_OFFICER_CODE") or DEMO_REVIEWER_CODE).strip()
DEMO_OFFICER_LABEL = (os.getenv("DEMO_OFFICER_LABEL") or "Demo officer").strip()[:80]
DEMO_OFFICER_ID = (os.getenv("DEMO_OFFICER_ID") or "officer-demo").strip()[:80]

ALLOW_DEMO_ADMIN = os.getenv("ALLOW_DEMO_ADMIN", "false").lower() in {"1", "true", "yes"}
DEMO_ADMIN_CODE = os.getenv("DEMO_ADMIN_CODE", "").strip()
DEMO_ADMIN_LABEL = (os.getenv("DEMO_ADMIN_LABEL") or "Demo administrator").strip()[:80]
DEMO_ADMIN_ID = (os.getenv("DEMO_ADMIN_ID") or "admin-demo").strip()[:80]


def _csv_env(name: str) -> set[str]:
    return {item.strip().lower() for item in os.getenv(name, "").split(",") if item.strip()}


GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_ADMIN_EMAILS = _csv_env("GOOGLE_ADMIN_EMAILS")
GOOGLE_OFFICER_EMAILS = _csv_env("GOOGLE_OFFICER_EMAILS")
GOOGLE_ADMIN_DOMAINS = _csv_env("GOOGLE_ADMIN_DOMAINS")
GOOGLE_OFFICER_DOMAINS = _csv_env("GOOGLE_OFFICER_DOMAINS")
GOOGLE_AUTH_ENABLED = bool(GOOGLE_CLIENT_ID)


class GoogleAuthConfigurationError(RuntimeError):
    pass


class GoogleCredentialError(ValueError):
    pass


class GoogleAccessDenied(PermissionError):
    pass


def _email_domain(email: str) -> str:
    return email.rsplit("@", 1)[1].lower() if "@" in email else ""


def _role_for_google_identity(email: str, hosted_domain: str = "") -> Optional[str]:
    """Map a verified Google identity to a role.

    Exact emails work for Gmail or Workspace accounts. Domain-wide mapping is accepted
    only when Google supplies a matching hosted-domain (``hd``) claim, so a consumer
    Google account using a custom email address cannot impersonate Workspace membership.
    """
    email = (email or "").strip().lower()
    hosted_domain = (hosted_domain or "").strip().lower()
    if email in GOOGLE_ADMIN_EMAILS:
        return "admin"
    if email in GOOGLE_OFFICER_EMAILS:
        return "officer"
    if hosted_domain and hosted_domain in GOOGLE_ADMIN_DOMAINS:
        return "admin"
    if hosted_domain and hosted_domain in GOOGLE_OFFICER_DOMAINS:
        return "officer"
    return None


def google_auth_public_config() -> Dict[str, Any]:
    return {
        "enabled": GOOGLE_AUTH_ENABLED,
        "client_id": GOOGLE_CLIENT_ID if GOOGLE_AUTH_ENABLED else "",
        "staff_allowlist_configured": bool(
            GOOGLE_ADMIN_EMAILS or GOOGLE_OFFICER_EMAILS or GOOGLE_ADMIN_DOMAINS or GOOGLE_OFFICER_DOMAINS
        ),
    }


def verify_google_credential(credential: str) -> Dict[str, Any]:
    if not GOOGLE_AUTH_ENABLED:
        raise GoogleAuthConfigurationError("Google Sign-In is not configured on this deployment")
    if not credential or len(credential) > 12000:
        raise GoogleCredentialError("Google credential is missing or invalid")
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token as google_id_token
    except ImportError as exc:
        raise GoogleAuthConfigurationError("Google authentication dependency is not installed") from exc
    try:
        info = google_id_token.verify_oauth2_token(
            credential, google_requests.Request(), GOOGLE_CLIENT_ID
        )
    except Exception as exc:
        raise GoogleCredentialError("Google identity token could not be verified") from exc
    if info.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
        raise GoogleCredentialError("Google identity token issuer is invalid")
    if info.get("email_verified") is not True:
        raise GoogleCredentialError("Google account email is not verified")
    email = str(info.get("email") or "").strip().lower()
    subject = str(info.get("sub") or "").strip()
    if not email or not subject:
        raise GoogleCredentialError("Google identity token is missing required identity claims")
    return info


def google_staff_login(credential: str) -> Dict[str, Any]:
    info = verify_google_credential(credential)
    email = str(info.get("email") or "").strip().lower()
    role = _role_for_google_identity(email, str(info.get("hd") or ""))
    if role is None:
        raise GoogleAccessDenied("This Google account is authenticated but is not approved for LableLens staff access")
    name = str(info.get("name") or "").strip()
    label = (f"{name} · {email}" if name else email)[:80]
    subject = str(info.get("sub") or "").strip()
    return issue_session(
        role=role,
        label=label,
        actor_id=f"google:{subject}"[:80],
        email=email,
        auth_provider="google",
    )


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_session(role: str = "guest", label: str = "Guest", actor_id: Optional[str] = None, email: Optional[str] = None, auth_provider: str = "local") -> Dict[str, Any]:
    role = role if role in {"guest", "officer", "admin"} else "guest"
    now = int(time.time())
    sid = secrets.token_urlsafe(18)
    payload = {
        "sid": sid,
        "actor_id": (actor_id or (sid if role == "guest" else f"{role}-{sid[:8]}"))[:80],
        "role": role,
        "label": (label or role.title())[:80],
        "email": (email or "")[:320],
        "auth_provider": (auth_provider or "local")[:32],
        "iat": now,
        "exp": now + SESSION_TTL_SECONDS,
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = _b64e(hmac.new(_SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    return {"token": f"{body}.{sig}", **payload}


def verify_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        body, sig = token.split(".", 1)
        expected = _b64e(hmac.new(_SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_b64d(body).decode("utf-8"))
        if int(payload.get("exp", 0)) <= int(time.time()):
            return None
        if not payload.get("sid") or payload.get("role") not in {"guest", "officer", "admin"}:
            return None
        return payload
    except Exception:
        return None


def staff_login(role: str, code: str) -> Optional[Dict[str, Any]]:
    role = (role or "").strip().lower()
    candidate = (code or "").strip()
    if role == "admin":
        if ALLOW_DEMO_ADMIN and DEMO_ADMIN_CODE and hmac.compare_digest(candidate, DEMO_ADMIN_CODE):
            return issue_session(role="admin", label=DEMO_ADMIN_LABEL, actor_id=DEMO_ADMIN_ID)
        return None
    if role == "officer":
        if ALLOW_DEMO_OFFICER and DEMO_OFFICER_CODE and hmac.compare_digest(candidate, DEMO_OFFICER_CODE):
            return issue_session(role="officer", label=DEMO_OFFICER_LABEL, actor_id=DEMO_OFFICER_ID)
        return None
    return None


def reviewer_login(code: str) -> Optional[Dict[str, Any]]:
    """Backward-compatible reviewer login; reviewer sessions are now officer sessions."""
    return staff_login("officer", code)
