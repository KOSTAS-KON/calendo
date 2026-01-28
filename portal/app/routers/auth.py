from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import quote, unquote

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
import bcrypt

from app.db import SessionLocal


router = APIRouter(tags=["auth"])


def _safe_next(next_path: str) -> str:
    if not next_path:
        return "/"
    try:
        nxt = unquote(next_path)
    except Exception:
        nxt = next_path
    if not nxt.startswith("/"):
        return "/"
    if nxt.startswith("//"):
        return "/"
    return nxt


def _extract_tenant_slug_from_next(next_path: str) -> str:
    nxt = _safe_next(next_path)
    parts = [p for p in nxt.split("/") if p]
    if len(parts) >= 2 and parts[0] == "t":
        return parts[1]
    return "default"


def _hash_check(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def _session_set(request: Request, key: str, value) -> None:
    sess = request.scope.get("session")
    if isinstance(sess, dict):
        sess[key] = value


def _session_clear(request: Request) -> None:
    sess = request.scope.get("session")
    if isinstance(sess, dict):
        sess.clear()


@router.get("/auth/ping")
def ping():
    return {"ok": True}


def _render_login_page(next_path: str, tenant_slug: str, error: str) -> HTMLResponse:
    msg = ""
    if error:
        msg = """
        <div style="margin:10px 0; padding:10px; border-radius:12px;
                    background:#3b0a0a; border:1px solid rgba(239,68,68,.5); color:#fecaca;">
          <b>Login failed:</b> Please check email/password.
        </div>
        """

    html = f"""
    <!doctype html>
    <html>
      <head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
        <title>Login</title>
        <style>
          body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial; background:#0b1220; color:#e5e7eb; margin:0;}}
          .wrap{{max-width:520px; margin:0 auto; padding:46px 18px;}}
          .card{{background:#101a2f; border:1px solid rgba(255,255,255,.08); border-radius:16px; padding:22px;}}
          input{{width:100%; padding:10px; border-radius:10px; border:1px solid rgba(255,255,255,.18); background:#0b1220; color:#e5e7eb; margin-top:8px;}}
          button{{margin-top:12px; padding:10px 14px; border-radius:10px; border:none; background:#2563eb; color:white; font-weight:900; width:100%;}}
          .hint{{margin-top:10px; opacity:.8; font-size:13px;}}
          code{{background:rgba(255,255,255,.08); padding:2px 6px; border-radius:6px;}}
        </style>
      </head>
      <body>
        <div class="wrap">
          <div class="card">
            <h2 style="margin:0 0 6px 0;">Sign in</h2>
            <div class="hint">Tenant: <code>{tenant_slug}</code></div>
            {msg}
            <form method="post" action="/auth/login">
              <input type="hidden" name="next" value="{quote(next_path)}"/>
              <input name="email" placeholder="Email" autocomplete="username" />
              <input name="password" placeholder="Password" autocomplete="current-password" type="password" />
              <button type="submit">Log in</button>
            </form>
          </div>
        </div>
      </body>
    </html>
    """
    return HTMLResponse(html)


def _get_client_ip(request: Request) -> str:
    # Prefer X-Forwarded-For (Render), fall back to client host
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _rate_limit_login(db, ip: str) -> tuple[bool, int]:
    """
    Returns (allowed, retry_after_seconds). DB-backed per-IP limiter.

    Requires model app.models.auth_rate_limit.AuthRateLimit
    with fields: ip, window_start, attempts, blocked_until.
    """
    from app.models.auth_rate_limit import AuthRateLimit

    # Defaults (can be overridden by env in your implementation elsewhere; keep simple here)
    max_attempts = int((os.getenv("LOGIN_RATE_LIMIT_COUNT") or "10").strip())
    window_seconds = int((os.getenv("LOGIN_RATE_LIMIT_WINDOW_SECONDS") or "600").strip())
    block_seconds = int((os.getenv("LOGIN_RATE_LIMIT_BLOCK_SECONDS") or "900").strip())

    now = datetime.utcnow()

    row = db.query(AuthRateLimit).filter(AuthRateLimit.ip == ip).first()
    if not row:
        row = AuthRateLimit(
            ip=ip,
            window_start=now,
            attempts=0,
            blocked_until=None,
            updated_at=now,
        )
        db.add(row)
        db.commit()
        db.refresh(row)

    # If blocked
    if row.blocked_until and row.blocked_until > now:
        retry_after = int((row.blocked_until - now).total_seconds())
        return False, max(retry_after, 1)

    # Reset window
    if row.window_start and row.window_start + timedelta(seconds=window_seconds) < now:
        row.window_start = now
        row.attempts = 0
        row.blocked_until = None
        row.updated_at = now
        db.add(row)
        db.commit()
        db.refresh(row)

    # Allowed for now
    return True, 0


def _record_login_failure(db, ip: str) -> None:
    from app.models.auth_rate_limit import AuthRateLimit

    max_attempts = int((os.getenv("LOGIN_RATE_LIMIT_COUNT") or "10").strip())
    block_seconds = int((os.getenv("LOGIN_RATE_LIMIT_BLOCK_SECONDS") or "900").strip())

    now = datetime.utcnow()

    row = db.query(AuthRateLimit).filter(AuthRateLimit.ip == ip).first()
    if not row:
        row = AuthRateLimit(ip=ip, window_start=now, attempts=0, blocked_until=None, updated_at=now)
        db.add(row)
        db.commit()
        db.refresh(row)

    row.attempts = int(row.attempts or 0) + 1
    row.updated_at = now

    if row.attempts >= max_attempts:
        row.blocked_until = now + timedelta(seconds=block_seconds)

    db.add(row)
    db.commit()


@router.get("/auth/login", response_class=HTMLResponse)
def auth_login_get(request: Request, next: str = "/t/default/suite", error: str = ""):
    next_path = _safe_next(next)
    tenant_slug = _extract_tenant_slug_from_next(next_path)
    return _render_login_page(next_path, tenant_slug, error)


@router.get("/login", response_class=HTMLResponse)
def login_get(request: Request, next: str = "/t/default/suite", error: str = ""):
    next_path = _safe_next(next)
    tenant_slug = _extract_tenant_slug_from_next(next_path)
    return _render_login_page(next_path, tenant_slug, error)


@router.post("/auth/login")
def auth_login_post(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form("/t/default/suite"),
):
    next_path = _safe_next(next)
    tenant_slug = _extract_tenant_slug_from_next(next_path)

    email = (email or "").strip().lower()
    password = password or ""
    if not email or not password:
        return RedirectResponse(url=f"/auth/login?next={quote(next_path)}&error=1", status_code=303)

    ip = _get_client_ip(request)

    db = SessionLocal()
    try:
        # Rate limit check
        allowed, retry_after = _rate_limit_login(db, ip)
        if not allowed:
            # keep generic error (avoid leaking info)
            return RedirectResponse(url=f"/auth/login?next={quote(next_path)}&error=1", status_code=303)

        from app.models.tenant import Tenant
        from app.models.user import User

        t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
        if not t:
            _record_login_failure(db, ip)
            return RedirectResponse(url=f"/auth/login?next={quote(next_path)}&error=1", status_code=303)

        u = db.query(User).filter(User.tenant_id == t.id, User.email == email).first()
        if not u or not getattr(u, "is_active", True):
            _record_login_failure(db, ip)
            return RedirectResponse(url=f"/auth/login?next={quote(next_path)}&error=1", status_code=303)

        if not _hash_check(password, u.password_hash):
            _record_login_failure(db, ip)
            return RedirectResponse(url=f"/auth/login?next={quote(next_path)}&error=1", status_code=303)

        # Success session
        _session_set(request, "user_id", u.id)
        _session_set(request, "tenant_id", u.tenant_id)
        _session_set(request, "tenant_slug", tenant_slug)
        _session_set(request, "role", u.role)
        _session_set(request, "email", u.email)
        _session_set(request, "logged_in_at", datetime.utcnow().isoformat())

        if hasattr(u, "last_login_at"):
            u.last_login_at = datetime.utcnow()
            db.add(u)
            db.commit()

        return RedirectResponse(url=next_path, status_code=303)

    finally:
        db.close()


@router.post("/login")
def login_post(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form("/t/default/suite"),
):
    return auth_login_post(request=request, email=email, password=password, next=next)


@router.get("/auth/logout")
def auth_logout(request: Request):
    _session_clear(request)
    return RedirectResponse(url="/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    _session_clear(request)
    return RedirectResponse(url="/", status_code=303)
