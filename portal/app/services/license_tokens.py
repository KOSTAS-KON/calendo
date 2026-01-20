from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def _b64url_decode(data: str) -> bytes:
    s = (data or "").strip().replace("-", "+").replace("_", "/")
    # pad
    pad = "=" * ((4 - (len(s) % 4)) % 4)
    return base64.b64decode(s + pad)


def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("utf-8").rstrip("=")


def _load_public_key(public_key_b64: str) -> Ed25519PublicKey:
    raw = _b64url_decode(public_key_b64)
    if len(raw) != 32:
        raise ValueError("LICENSE_PUBLIC_KEY must decode to 32 bytes (Ed25519 public key)")
    return Ed25519PublicKey.from_public_bytes(raw)


@dataclass
class LicensePayload:
    v: int
    client_id: str
    plan: int
    mode: str
    issued_at: datetime
    expires_at: datetime
    nonce: str

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "LicensePayload":
        def parse_dt(x: Any) -> datetime:
            # Accept ISO8601 or unix timestamp
            if isinstance(x, (int, float)):
                return datetime.fromtimestamp(float(x), tz=timezone.utc)
            if not isinstance(x, str):
                raise ValueError("Invalid datetime")
            s = x.strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)

        return LicensePayload(
            v=int(d.get("v", 1)),
            client_id=str(d.get("client_id", "")).strip(),
            plan=int(d.get("plan", 0)),
            mode=str(d.get("mode", "BOTH")).upper().strip(),
            issued_at=parse_dt(d.get("issued_at")),
            expires_at=parse_dt(d.get("expires_at")),
            nonce=str(d.get("nonce", "")).strip(),
        )


def verify_activation_code(code: str, public_key_b64: str) -> LicensePayload:
    """Verify and decode an activation code.

    Format:  <prefix?>.<payload_b64url>.<sig_b64url>
    We accept optional prefixes like "TSL1" before the first dot.

    Signature is Ed25519 over the ASCII bytes of payload_b64url.
    """
    if not public_key_b64:
        raise ValueError("LICENSE_PUBLIC_KEY is not set")

    parts = (code or "").strip().split(".")
    if len(parts) == 2:
        payload_b64, sig_b64 = parts
    elif len(parts) == 3:
        _prefix, payload_b64, sig_b64 = parts
    else:
        raise ValueError("Invalid activation code format")

    payload_b64 = payload_b64.strip()
    sig_b64 = sig_b64.strip()
    if not payload_b64 or not sig_b64:
        raise ValueError("Invalid activation code format")

    pub = _load_public_key(public_key_b64)
    sig = _b64url_decode(sig_b64)
    try:
        pub.verify(sig, payload_b64.encode("utf-8"))
    except Exception as e:
        raise ValueError("Activation code signature invalid") from e

    payload_raw = _b64url_decode(payload_b64)
    try:
        d = json.loads(payload_raw.decode("utf-8"))
    except Exception as e:
        raise ValueError("Activation code payload invalid") from e

    p = LicensePayload.from_dict(d)
    if not p.client_id:
        raise ValueError("Activation code missing client_id")
    if p.mode not in {"PORTAL", "SMS", "BOTH"}:
        raise ValueError("Activation code has invalid mode")
    if p.plan not in {1, 2, 3}:
        raise ValueError("Activation code has invalid plan")

    now = datetime.now(tz=timezone.utc)
    if p.expires_at <= now:
        raise ValueError("Activation code is expired")
    if p.expires_at <= p.issued_at:
        raise ValueError("Activation code expiry invalid")

    return p


def format_env_line_block(payload_b64: str, sig_b64: str) -> str:
    """Helper for issuer tools."""
    return f"TSL1.{payload_b64}.{sig_b64}"


def make_payload_b64(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return _b64url_encode(raw)
