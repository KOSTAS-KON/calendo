from __future__ import annotations

import json
import http.client
import os
from pathlib import Path
from typing import Any, Dict, Optional

from src.settings.secrets import get_infobip_secrets


def _normalize_base_url(raw: str) -> str:
    """
    Infobip base URL should be ONLY the host for http.client:
      ✅ "xxxx.api.infobip.com"
      ❌ "https://xxxx.api.infobip.com"
      ❌ "xxxx.api.infobip.com/"
    """
    if not raw:
        return ""
    s = str(raw).strip()
    s = s.replace("https://", "").replace("http://", "")
    s = s.split("/")[0].strip()
    return s


class InfobipHttpClient:
    """Infobip SMS client using http.client (no requests dependency).

    Uses:
      - config/secrets.yaml (recommended), via src.settings.secrets
      - OR environment variables INFOBIP_BASEURL / INFOBIP_API_KEY (fallback)
    """

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        s = get_infobip_secrets()

        resolved_base = base_url or s.base_url or os.environ.get("INFOBIP_BASEURL", "")
        resolved_key = api_key or s.api_key or os.environ.get("INFOBIP_API_KEY", "")

        self.base_url = _normalize_base_url(resolved_base)
        self.api_key = str(resolved_key or "").strip()

        if not self.base_url:
            raise ValueError(
                "Missing Infobip base URL. Set in config/secrets.yaml (infobip.base_url) or INFOBIP_BASEURL."
            )
        if not self.api_key:
            raise ValueError(
                "Missing Infobip API key. Set in config/secrets.yaml (infobip.api_key) or INFOBIP_API_KEY."
            )

    @classmethod
    def from_secrets(cls) -> "InfobipHttpClient":
        # Secrets are already read inside __init__ via get_infobip_secrets()
        return cls()

    @classmethod
    def from_env(cls) -> "InfobipHttpClient":
        return cls(base_url=os.environ.get("INFOBIP_BASEURL", ""), api_key=os.environ.get("INFOBIP_API_KEY", ""))

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"App {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def send_sms_advanced(self, to: str, sender: str, text: str) -> Dict[str, Any]:
        """Send an SMS using /sms/2/text/advanced.

        Returns dict: {status_code:int, body:str, json:dict|None}
        """
        conn = http.client.HTTPSConnection(self.base_url, timeout=30)

        payload = json.dumps(
            {
                "messages": [
                    {
                        "destinations": [{"to": str(to)}],
                        "from": str(sender),
                        "text": str(text),
                    }
                ]
            }
        )

        conn.request("POST", "/sms/2/text/advanced", payload, self._headers())
        res = conn.getresponse()
        body = res.read().decode("utf-8", errors="replace")

        parsed = None
        try:
            parsed = json.loads(body) if body else None
        except Exception:
            parsed = None

        return {"status_code": int(res.status), "body": body, "json": parsed}

    def get_logs(self, message_id: str) -> Dict[str, Any]:
        """Fetch logs for a messageId via /sms/1/logs?messageId=..."""
        conn = http.client.HTTPSConnection(self.base_url, timeout=30)
        path = f"/sms/1/logs?messageId={message_id}"
        conn.request("GET", path, headers={"Authorization": f"App {self.api_key}", "Accept": "application/json"})
        res = conn.getresponse()
        body = res.read().decode("utf-8", errors="replace")
        parsed = None
        try:
            parsed = json.loads(body) if body else None
        except Exception:
            parsed = None
        return {"status_code": int(res.status), "body": body, "json": parsed}

    @staticmethod
    def extract_message_id(send_json: Any) -> str:
        """Extract messageId from Infobip send response JSON."""
        if not send_json:
            return ""
        try:
            msgs = send_json.get("messages") or []
            if msgs and isinstance(msgs, list):
                return str(msgs[0].get("messageId") or "")
        except Exception:
            return ""
        return ""


# --- Backwards-compatible alias (safe, no args, never crashes) ---
if not hasattr(InfobipHttpClient, "from_yaml_secrets"):

    @classmethod
    def _from_yaml_secrets(cls) -> "InfobipHttpClient":
        # Historically some code looked for a root-level secrets.yaml.
        # We don't need to parse it here: get_infobip_secrets() already handles your app config.
        return cls.from_secrets()

    InfobipHttpClient.from_yaml_secrets = _from_yaml_secrets
