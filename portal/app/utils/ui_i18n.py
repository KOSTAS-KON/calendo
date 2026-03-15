from __future__ import annotations

from urllib.parse import urlencode

from fastapi import Request
from fastapi.templating import Jinja2Templates

SUPPORTED_UI_LANGS = {"el", "en"}
DEFAULT_UI_LANG = "el"


def _session_dict(request: Request) -> dict:
    try:
        s = request.session
    except Exception:
        s = request.scope.get("session")
    return s if isinstance(s, dict) else {}


def ui_lang(request: Request) -> str:
    s = _session_dict(request)
    requested = str(request.query_params.get("lang") or "").strip().lower()
    if requested in SUPPORTED_UI_LANGS:
        s["ui_lang"] = requested
        return requested

    stored = str(s.get("ui_lang") or "").strip().lower()
    if stored in SUPPORTED_UI_LANGS:
        return stored

    s["ui_lang"] = DEFAULT_UI_LANG
    return DEFAULT_UI_LANG


def tr(request: Request, el_text: str, en_text: str | None = None) -> str:
    if ui_lang(request) == "en":
        return en_text if en_text is not None else el_text
    return el_text


def lang_switch_url(request: Request, lang: str) -> str:
    lang = (lang or "").strip().lower()
    if lang not in SUPPORTED_UI_LANGS:
        lang = DEFAULT_UI_LANG

    pairs = [(k, v) for k, v in request.query_params.multi_items() if k.lower() != "lang"]
    pairs.append(("lang", lang))
    query = urlencode(pairs, doseq=True)
    return f"{request.url.path}?{query}" if query else request.url.path


def register_template_helpers(templates: Jinja2Templates) -> None:
    templates.env.globals["ui_lang"] = ui_lang
    templates.env.globals["tr"] = tr
    templates.env.globals["lang_switch_url"] = lang_switch_url
