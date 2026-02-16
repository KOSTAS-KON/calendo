"""Project path helpers.

Render / Docker / local dev may run the app with different current-working
directories. Any relative paths (e.g. ``app/static``) can break when the CWD is
not the portal package root.

These helpers compute absolute paths anchored on the *installed code* location
so the app works regardless of where it is started from.
"""

from __future__ import annotations

from pathlib import Path


# .../portal/app/utils/paths.py
UTILS_DIR = Path(__file__).resolve().parent

# .../portal/app
APP_DIR = UTILS_DIR.parent

# .../portal
PORTAL_DIR = APP_DIR.parent

STATIC_DIR = APP_DIR / "static"
TEMPLATES_DIR = APP_DIR / "templates"
