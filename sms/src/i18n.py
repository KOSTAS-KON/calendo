from __future__ import annotations
from pathlib import Path
import yaml

_CACHE: dict[str, dict] = {}

def load_lang(code: str = "el") -> dict:
    if code in _CACHE:
        return _CACHE[code]
    path = Path("config") / "i18n" / f"{code}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    _CACHE[code] = data
    return data

def t(lang: dict, *keys: str, default: str = "") -> str:
    cur = lang
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur if isinstance(cur, str) else default
