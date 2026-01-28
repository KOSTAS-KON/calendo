from __future__ import annotations

import os
from datetime import datetime
from urllib.parse import quote, unquote

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
import bcrypt

from app.db import SessionLocal

# Login rate limiting (DB-backed)
LOGIN_RATE_LIMIT_COUNT = int(os.getenv('LOGIN_RATE_LIMIT_COUNT') or '10')
LOGIN_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv('LOGIN_RATE_LIMIT_WINDOW_SECONDS') or str(10*60))
LOGIN_RATE_LIMIT_BLOCK_SECONDS = int(os.getenv('LOGIN_RATE_LIMIT_BLOCK_SECONDS') or str(15*60))



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


def _hash_make(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _session_set(request: Request, key: str, value) -> None:
    sess = request.scope.get("session")
    if isinstance(sess, dict):
        sess[key] = value


def _session_get(request: Request, key: str, default=None):
    sess = request.scope.get("session")
    if isinstance(sess, dict):
        return sess.get(key, default)
    return default


def _session_clear(request: Request) -> None:
    sess = request.scope.get("session")
    if isinstance(sess, dict):
        sess.clear()


# ---- rate limit helpers ----
def _client_ip(request: Request) -> str:
    xf = (request.headers.get('x-forwarded-for') or '').split(',')[0].strip()
    if xf:
        return xf
    return (request.client.host if request.client else 'unknown')

def _rate_limit_check(db, ip: str) -> tuple[bool, int]:
    \"\"\"Returns (allowed, retry_after_seconds). DB-backed per-IP limiter.\"\"\"
    from app.models.auth_rate_limit import AuthRateLimit
    now = datetime.utcnow()
    rl = db.query(AuthRateLimit).filter(AuthRateLimit.ip == ip).first()
    if rl and rl.blocked_until and rl.blocked_until > now:
        return (False, int((rl.blocked_until - now).total_seconds()))
    if not rl:
        rl = AuthRateLimit(ip=ip, window_start=now, count=0, blocked_until=None)
        db.add(rl)
        db.commit()
        db.refresh(rl)
    # reset window if expired
    if rl.window_start and (now - rl.window_start).total_seconds() > LOGIN_RATE_LIMIT_WINDOW_SECONDS:
        rl.window_start = now
        rl.count = 0
        rl.blocked_until = None
    rl.count = int(rl.count or 0) + 1
    allowed = rl.count <= LOGIN_RATE_LIMIT_COUNT
    if not allowed:
        rl.blocked_until = now + timedelta(seconds=LOGIN_RATE_LIMIT_BLOCK_SECONDS)
    db.add(rl)
    db.commit()
    retry = 0
    if rl.blocked_until and rl.blocked_until > now:
        retry = int((rl.blocked_until - now).total_seconds())
    return (allowed, retry)


@router.get("/auth/ping")
def ping():
    return {"ok": True}


def _render_login_page(next_path: str, tenant_slug: str, error: str) -> HTMLResponse:
    msg = ""
    if error:
        if str(error) == 'rate_limited':
            msg = """
        <div style=\"margin:10px 0; padding:10px; border-radius:12px;
                    background:#3b0a0a; border:1px solid rgba(239,68,68,.5); color:#fecaca;\">
          <b>Too many attempts.</b> Please wait a few minutes and try again.
        </div>
        """
        else:
            msg = """
        <div style=\"margin:10px 0; padding:10px; border-radius:12px;
                    background:#3b0a0a; border:1px solid rgba(239,68,68,.5); color:#fecaca;\">
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

    db = SessionLocal()
    try:
        ip = _client_ip(request)
        allowed, retry_after = _rate_limit_check(db, ip)
        if not allowed:
            return RedirectResponse(url=f"/auth/login?next={quote(next_path)}&error=rate_limited", status_code=303)

        from app.models.tenant import Tenant
        from app.models.user import User

        t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
        if not t:
            return RedirectResponse(url=f"/auth/login?next={quote(next_path)}&error=1", status_code=303)

        u = db.query(User).filter(User.tenant_id == t.id, User.email == email).first()
        if not u or not getattr(u, "is_active", True):
            return RedirectResponse(url=f"/auth/login?next={quote(next_path)}&error=1", status_code=303)

        if not _hash_check(password, u.password_hash):
            return RedirectResponse(url=f"/auth/login?next={quote(next_path)}&error=1", status_code=303)

        _session_set(request, "user_id", u.id)
        _session_set(request, "tenant_id", u.tenant_id)
        _session_set(request, "tenant_slug", tenant_slug)
        _session_set(request, "role", u.role)
        _session_set(request, "email", u.email)
        _session_set(request, "must_reset_password", bool(getattr(u, "must_reset_password", False)))
        _session_set(request, "logged_in_at", datetime.utcnow().isoformat())

        if hasattr(u, "last_login_at"):
            u.last_login_at = datetime.utcnow()
            db.add(u)
            db.commit()

        # Force password change if required
        if bool(getattr(u, "must_reset_password", False)):
            return RedirectResponse(url=f"/auth/change-password?next={quote(next_path)}", status_code=303)

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


# ------------------------
# Password reset (enforced)
# ------------------------
def _render_change_password(next_path: str, error: str = "", ok: str = "") -> HTMLResponse:
    banner = ""
    if error:
        banner = """
        <div style="margin:10px 0; padding:10px; border-radius:12px;
                    background:#3b0a0a; border:1px solid rgba(239,68,68,.5); color:#fecaca;">
          <b>Password change failed:</b> Please try again.
        </div>
        """
    if ok:
        banner = """
        <div style="margin:10px 0; padding:10px; border-radius:12px;
                    background:#052e16; border:1px solid rgba(34,197,94,.45); color:#bbf7d0;">
          <b>Password updated successfully.</b>
        </div>
        """

    html = f"""
    <!doctype html>
    <html>
      <head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
        <title>Change Password</title>
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
            <h2 style="margin:0 0 6px 0;">Change Password</h2>
            <div class="hint">For security, you must set a new password before continuing.</div>
            {banner}
            <form method="post" action="/auth/change-password">
              <input type="hidden" name="next" value="{quote(next_path)}"/>
              <input name="new_password" placeholder="New password" type="password" autocomplete="new-password"/>
              <input name="confirm_password" placeholder="Confirm new password" type="password" autocomplete="new-password"/>
              <button type="submit">Update password</button>
            </form>
            <div class="hint">Next: <code>{next_path}</code></div>
          </div>
        </div>
      </body>
    </html>
    """
    return HTMLResponse(html)


@router.get("/auth/change-password", response_class=HTMLResponse)
def change_password_get(request: Request, next: str = "/t/default/suite", error: str = "", ok: str = ""):
    next_path = _safe_next(next)
    return _render_change_password(next_path, error=error, ok=ok)


@router.post("/auth/change-password")
def change_password_post(
    request: Request,
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    next: str = Form("/t/default/suite"),
):
    next_path = _safe_next(next)

    if not new_password or new_password != confirm_password or len(new_password) < 8:
        return RedirectResponse(url=f"/auth/change-password?next={quote(next_path)}&error=1", status_code=303)

    user_id = _session_get(request, "user_id")
    if not user_id:
        return RedirectResponse(url=f"/auth/login?next={quote(next_path)}", status_code=303)

    db = SessionLocal()
    try:
        ip = _client_ip(request)
        allowed, retry_after = _rate_limit_check(db, ip)
        if not allowed:
            return RedirectResponse(url=f"/auth/login?next={quote(next_path)}&error=rate_limited", status_code=303)

        from app.models.user import User
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            return RedirectResponse(url=f"/auth/login?next={quote(next_path)}", status_code=303)

        u.password_hash = _hash_make(new_password)
        if hasattr(u, "must_reset_password"):
            u.must_reset_password = False

        db.add(u)
        db.commit()

        _session_set(request, "must_reset_password", False)

        return RedirectResponse(url=next_path, status_code=303)
    finally:
        db.close()


# Logout (both paths)
@router.get("/auth/logout")
def auth_logout(request: Request):
    _session_clear(request)
    return RedirectResponse(url="/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    _session_clear(request)
    return RedirectResponse(url="/", status_code=303)
