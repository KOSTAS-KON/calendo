from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


from src.core.paths import PROJECT_ROOT

DEFAULT_SECRETS_PATH = PROJECT_ROOT / "config/secrets.yaml"


@dataclass(frozen=True)
class InfobipSecrets:
    base_url: str
    api_key: str
    sender: str

    def is_complete(self) -> bool:
        return bool(self.base_url and self.api_key and self.sender)


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)


def load_secrets(path: Path = DEFAULT_SECRETS_PATH) -> dict:
    return _read_yaml(path)


def save_infobip_secrets(base_url: str, api_key: str, sender: str, path: Path = DEFAULT_SECRETS_PATH) -> None:
    data = load_secrets(path)
    data["infobip"] = {
        "base_url": (base_url or "").strip(),
        "api_key": (api_key or "").strip(),
        "sender": (sender or "").strip(),
    }
    _write_yaml(path, data)


def get_infobip_secrets(path: Path = DEFAULT_SECRETS_PATH) -> InfobipSecrets:
    data = load_secrets(path)
    ib = (data.get("infobip") or {})
    return InfobipSecrets(
        base_url=str(ib.get("base_url") or "").strip(),
        api_key=str(ib.get("api_key") or "").strip(),
        sender=str(ib.get("sender") or "").strip(),
    )


def has_infobip_secrets(path: Path = DEFAULT_SECRETS_PATH) -> bool:
    return get_infobip_secrets(path).is_complete()
