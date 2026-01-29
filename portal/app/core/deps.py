from __future__ import annotations

import os
from fastapi import HTTPException, Request


def session_dict(request: Request) -> dict:
    s = request.scope.get("session")
    return s if isinstance(s, dict) else {}


def expected_admin_key() -> str:
    return (os.getenv("ADMIN_KEY") or "").strip()


def admin_key_from_request(request: Request) -> str:
    # Prefer header, then session, then (legacy) query param.
    hdr = (request.headers.get("X-Admin-Key") or request.headers.get("x-admin-key") or "").strip()
    if hdr:
        return hdr
    s = session_dict(request)
    if s.get("admin_key"):
        return str(s.get("admin_key") or "").strip()
    q = (request.query_params.get("admin_key") or "").strip()
    return q


def require_admin(request: Request) -> None:
    # Role-based access first (preferred)
    s = session_dict(request)
    role = str(s.get("role") or "").lower()
    if role in ("admin", "owner"):
        return

    # Fallback: ADMIN_KEY
    exp = expected_admin_key()
    if not exp:
        raise HTTPException(status_code=403, detail="Forbidden")
    got = admin_key_from_request(request)
    if got != exp:
        raise HTTPException(status_code=403, detail="Forbidden")
