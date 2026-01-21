from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
import requests

from src.core.paths import PROJECT_ROOT

DEFAULT_SECRETS_PATH = PROJECT_ROOT / "config/secrets.yaml"


@dataclass(frozen=True)
class InfobipSecrets:
    base_url: str
    api_key: str
    sender: str

    def is_complete(self) -> bool:
        return bool(self.base_url and self.api_key and self.sender)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _from_env() -> InfobipSecrets:
    return InfobipSecrets(
        base_url=str(os.getenv("INFOBIP_BASE_URL", "")).strip(),
        api_key=str(os.getenv("INFOBIP_API_KEY", "")).strip(),
        sender=str(os.getenv("INFOBIP_SENDER", "")).strip(),
    )


def _from_portal() -> InfobipSecrets:
    """Fetch Infobip credentials from the Portal (recommended for SaaS).

    Requires:
      - PORTAL_BASE_URL (e.g. https://calendo-3ktr.onrender.com)
      - INTERNAL_API_KEY (shared secret; same value on Portal + SMS service)
    """
    portal = str(os.getenv("PORTAL_BASE_URL", "")).strip().rstrip("/")
    internal_key = str(os.getenv("INTERNAL_API_KEY", "")).strip()

    if not portal or not internal_key:
        return InfobipSecrets(base_url="", api_key="", sender="")

    url = f"{portal}/api/internal/clinic_settings"
    try:
        r = requests.get(url, headers={"X-Internal-Key": internal_key}, timeout=10)
        if r.status_code != 200:
            return InfobipSecrets(base_url="", api_key="", sender="")
        data = r.json() or {}
        clinic = (data.get("clinic") or {})
        return InfobipSecrets(
            base_url=str(clinic.get("infobip_base_url") or "").strip(),
            api_key=str(clinic.get("infobip_api_key") or "").strip(),
            sender=str(clinic.get("infobip_sender") or "").strip(),
        )
    except Exception:
        return InfobipSecrets(base_url="", api_key="", sender="")


def _from_yaml(path: Path) -> InfobipSecrets:
    data = _load_yaml(path)
    ib = (data.get("infobip") or {})
    return InfobipSecrets(
        base_url=str(ib.get("base_url") or "").strip(),
        api_key=str(ib.get("api_key") or "").strip(),
        sender=str(ib.get("sender") or "").strip(),
    )


@lru_cache(maxsize=1)
def get_infobip_secrets(path: Path = DEFAULT_SECRETS_PATH) -> InfobipSecrets:
    """Return SMS provider secrets with a clear precedence order.

    Precedence:
      1) Environment variables (INFOBIP_BASE_URL / INFOBIP_API_KEY / INFOBIP_SENDER)
      2) Portal fetch (PORTAL_BASE_URL + INTERNAL_API_KEY)  <-- best for SaaS
      3) Local YAML file (config/secrets.yaml)              <-- best for on-prem trials

    Note: Cached for the process lifetime. Restart the service after changing settings.
    """
    env_sec = _from_env()
    if env_sec.is_complete():
        return env_sec

    portal_sec = _from_portal()
    if portal_sec.is_complete():
        return portal_sec

    return _from_yaml(path)


def has_infobip_secrets(path: Path = DEFAULT_SECRETS_PATH) -> bool:
    return get_infobip_secrets(path).is_complete()
