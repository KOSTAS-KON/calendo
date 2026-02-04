from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import quote, unquote

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
import bcrypt
import sqlalchemy as sa
import requests

from app.db import SessionLocal
from app.config import settings

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


def _turnstile_configured() -> bool:
    enabled = bool(getattr(settings, "TURNSTILE_ENABLED", False))
    site_key = (getattr(settings, "TURNSTILE_SITE_KEY", "") or "").strip()
    secret_key = (getattr(settings, "TURNSTILE_SECRET_KEY", "") or "").strip()
    if enabled and (not site_key or not secret_key):
<<<<<<< HEAD
        print("[auth] WARNING: TURNSTILE_ENABLED=true but keys missing. Turnstile will be bypassed until configured.")
=======
        print("[auth] WARNING: TURNSTILE_ENABLED=true but keys missing. Turnstile will be bypassed.")
>>>>>>> 6921369 (Admin: add reset password endpoint + temp password generator)
        return False
    return enabled and bool(site_key) and bool(secret_key)


def _render_login_page(next_path: str, tenant_slug: str, error: str) -> HTMLResponse:
    configured = _turnstile_configured()
    site_key = (getattr(settings, "TURNSTILE_SITE_KEY", "") or "").strip()

    msg = ""
    if error:
        if error == "bot":
            msg = """
            <div style="margin:10px 0; padding:10px; border-radius:12px;
                        background:#1f2937; border:1px solid rgba(148,163,184,.35); color:#e5e7eb;">
              <b>Security check failed:</b> Please retry. If this continues, contact the administrator.
            </div>
            """
        else:
            msg = """
            <div style="margin:10px 0; padding:10px; border-radius:12px;
                        background:#3b0a0a; border:1px solid rgba(239,68,68,.5); color:#fecaca;">
              <b>Login failed:</b> Please check email/password.
            </div>
            """

    turnstile_block = ""
    turnstile_script = ""
    if configured and site_key:
        turnstile_block = f"""
          <div style=\"margin-top:12px;\">
            <div class=\"cf-turnstile\" data-sitekey=\"{site_key}\"></div>
          </div>
        """
        turnstile_script = """
          <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
        """

    html = f"""
    <!doctype html>
    <html>
      <head><meta charset=\"utf-8\"/><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>
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
        <div class=\"wrap\">
          <div class=\"card\">
            <h2 style=\"margin:0 0 6px 0;\">Sign in</h2>
            <div class=\"hint\">Tenant: <code>{tenant_slug}</code></div>
            {msg}
            <form method=\"post\" action=\"/auth/login\">
              <input type=\"hidden\" name=\"next\" value=\"{quote(next_path)}\"/>
              <input name=\"email\" placeholder=\"Email\" autocomplete=\"username\" />
              <input name=\"password\" placeholder=\"Password\" autocomplete=\"current-password\" type=\"password\" />
              {turnstile_block}
              <button type=\"submit\">Log in</button>
            </form>
          </div>
        </div>
        {turnstile_script}
      </body>
    </html>
    """
    return HTMLResponse(html)


<<<<<<< HEAD
def _verify_turnstile_or_raise(token: str, ip: str) -> None:
=======
def _verify_turnstile(token: str, ip: str) -> tuple[bool, str]:
    """Verify Turnstile. Returns (ok, reason)."""
>>>>>>> 6921369 (Admin: add reset password endpoint + temp password generator)
    if not _turnstile_configured():
        return True, "bypassed"

    secret = (getattr(settings, "TURNSTILE_SECRET_KEY", "") or "").strip()
    token = (token or "").strip()
    if not token:
        return False, "missing_token"

    timeout = float(getattr(settings, "TURNSTILE_TIMEOUT_SECONDS", 5) or 5)
<<<<<<< HEAD

    resp = requests.post(
        "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        data={"secret": secret, "response": token, "remoteip": ip},
        timeout=timeout,
    )
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if not data.get("success"):
        # include error codes (helps debugging without breaking UX)
        raise ValueError(f"Turnstile verification failed: {data.get('error-codes')}")
=======
    try:
        resp = requests.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": secret, "response": token, "remoteip": ip},
            timeout=timeout,
        )
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if data.get("success"):
            return True, "ok"
        return False, f"failed:{data.get('error-codes')}"
    except Exception as e:
        return False, f"exception:{e}"
>>>>>>> 6921369 (Admin: add reset password endpoint + temp password generator)


def _get_client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _rate_limit_login(db, ip: str) -> tuple[bool, int]:
    from app.models.auth_rate_limit import AuthRateLimit

    max_attempts = int((getattr(settings, "LOGIN_RATE_LIMIT_COUNT", None) or 10))
    window_seconds = int((getattr(settings, "LOGIN_RATE_LIMIT_WINDOW_SECONDS", None) or 600))
    block_seconds = int((getattr(settings, "LOGIN_RATE_LIMIT_BLOCK_SECONDS", None) or 900))

    now = datetime.utcnow()
    row = db.query(AuthRateLimit).filter(AuthRateLimit.ip == ip).first()
    if not row:
        row = AuthRateLimit(ip=ip, window_start=now, count=0, blocked_until=None)
        db.add(row)
        db.commit()
        db.refresh(row)

    if row.blocked_until and row.blocked_until > now:
        retry_after = int((row.blocked_until - now).total_seconds())
        return False, max(retry_after, 1)

    if row.window_start and row.window_start + timedelta(seconds=window_seconds) < now:
        row.window_start = now
        row.count = 0
        row.blocked_until = None
        db.add(row)
        db.commit()
        db.refresh(row)

    return True, 0


def _record_login_failure(db, ip: str) -> None:
    from app.models.auth_rate_limit import AuthRateLimit

    max_attempts = int((getattr(settings, "LOGIN_RATE_LIMIT_COUNT", None) or 10))
    block_seconds = int((getattr(settings, "LOGIN_RATE_LIMIT_BLOCK_SECONDS", None) or 900))
    now = datetime.utcnow()

    row = db.query(AuthRateLimit).filter(AuthRateLimit.ip == ip).first()
    if not row:
        row = AuthRateLimit(ip=ip, window_start=now, count=0, blocked_until=None)
        db.add(row)
        db.commit()
        db.refresh(row)

    row.count = int(row.count or 0) + 1
    if row.count >= max_attempts:
        row.blocked_until = now + timedelta(seconds=block_seconds)

    db.add(row)
    db.commit()


def _get_tenant_id_by_slug(db, tenant_slug: str) -> str | None:
<<<<<<< HEAD
    row = db.execute(
        sa.text("SELECT id FROM tenants WHERE slug = :slug LIMIT 1"),
        {"slug": tenant_slug},
    ).fetchone()
=======
    row = db.execute(sa.text("SELECT id FROM tenants WHERE slug = :slug LIMIT 1"), {"slug": tenant_slug}).fetchone()
>>>>>>> 6921369 (Admin: add reset password endpoint + temp password generator)
    return row[0] if row else None


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
    cf_turnstile_response: str = Form("", alias="cf-turnstile-response"),
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
        allowed, _ = _rate_limit_login(db, ip)
        if not allowed:
            return RedirectResponse(url=f"/auth/login?next={quote(next_path)}&error=1", status_code=303)

<<<<<<< HEAD
        # ✅ TURNSTILE SOFT-FAIL:
        # If Turnstile is enabled but token delivery is flaky (common on some deployments),
        # do NOT block a correct login. Log the failure and proceed to password verification.
        if _turnstile_configured():
            try:
                _verify_turnstile_or_raise(cf_turnstile_response, ip)
            except Exception as e:
                # Still count towards rate limiting, but don't show bot error.
                _record_login_failure(db, ip)
                print(f"[auth] WARNING: Turnstile verification failed; allowing credential check. reason={e}")
=======
        # Turnstile soft-fail: don't block valid users due to flaky token delivery
        ok, reason = _verify_turnstile(cf_turnstile_response, ip)
        if not ok:
            _record_login_failure(db, ip)
            print(f"[auth] WARNING: turnstile not verified ({reason}); continuing with credential check")
>>>>>>> 6921369 (Admin: add reset password endpoint + temp password generator)

        from app.models.user import User

        tenant_id = _get_tenant_id_by_slug(db, tenant_slug)
        if not tenant_id:
            _record_login_failure(db, ip)
            return RedirectResponse(url=f"/auth/login?next={quote(next_path)}&error=1", status_code=303)

        u = (
            db.query(User)
<<<<<<< HEAD
            .filter(
                User.tenant_id == tenant_id,
                sa.func.lower(User.email) == email,
            )
=======
            .filter(User.tenant_id == tenant_id, sa.func.lower(User.email) == email)
>>>>>>> 6921369 (Admin: add reset password endpoint + temp password generator)
            .first()
        )
        if not u or not getattr(u, "is_active", True):
            _record_login_failure(db, ip)
            return RedirectResponse(url=f"/auth/login?next={quote(next_path)}&error=1", status_code=303)

        if not _hash_check(password, u.password_hash):
            _record_login_failure(db, ip)
            return RedirectResponse(url=f"/auth/login?next={quote(next_path)}&error=1", status_code=303)

        # normalize stored email (optional but helps long-term)
        if getattr(u, "email", "").lower() != email:
            try:
                u.email = email
                db.add(u)
                db.commit()
            except Exception:
                db.rollback()

        _session_set(request, "user_id", u.id)
        _session_set(request, "tenant_id", u.tenant_id)
        _session_set(request, "tenant_slug", tenant_slug)
        _session_set(request, "role", u.role)
        _session_set(request, "email", (u.email or "").lower())
        _session_set(request, "logged_in_at", datetime.utcnow().isoformat())

<<<<<<< HEAD
        if hasattr(u, "last_login_at"):
            try:
                u.last_login_at = datetime.utcnow()
                db.add(u)
                db.commit()
            except Exception:
                db.rollback()

=======
>>>>>>> 6921369 (Admin: add reset password endpoint + temp password generator)
        return RedirectResponse(url=next_path, status_code=303)
    finally:
        db.close()


@router.post("/login")
def login_post(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    cf_turnstile_response: str = Form("", alias="cf-turnstile-response"),
    next: str = Form("/t/default/suite"),
):
<<<<<<< HEAD
    return auth_login_post(
        request=request,
        email=email,
        password=password,
        cf_turnstile_response=cf_turnstile_response,
        next=next,
    )
=======
    return auth_login_post(request, email, password, cf_turnstile_response, next)
>>>>>>> 6921369 (Admin: add reset password endpoint + temp password generator)


@router.get("/auth/logout")
def auth_logout(request: Request):
    _session_clear(request)
    return RedirectResponse(url="/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    _session_clear(request)
    return RedirectResponse(url="/", status_code=303)
