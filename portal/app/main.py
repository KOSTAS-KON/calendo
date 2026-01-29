from __future__ import annotations

from datetime import datetime, timedelta
from app.dev_seed import ensure_test_users

import uuid
import os
import hashlib
from urllib.parse import quote, unquote

from fastapi import FastAPI, Request, Response, Form
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

import bcrypt

# Optional: Sentry
try:
    import sentry_sdk
except Exception:
    sentry_sdk = None

from app.db import SessionLocal
from app.routers.web import router as web_router
from app.routers.auth import router as auth_router
from app.routers.admin import router as admin_router
from app.api.public_alias import router as public_alias_router


app = FastAPI(title="Calendo Portal", version="1.0.0")

# Static
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# ✅ Seed test users BEFORE routers start handling requests
ensure_test_users()

# Routers
app.include_router(auth_router)
app.include_router(web_router)
app.include_router(admin_router)

# ✅ Adds /api/clinic_settings and /api/license
app.include_router(public_alias_router)

# (rest of your existing main.py continues unchanged below)
