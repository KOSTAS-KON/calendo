from __future__ import annotations

import argparse
import base64
import json
import secrets
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def b64u_decode(s: str) -> bytes:
    ss = (s or "").strip().replace("-", "+").replace("_", "/")
    pad = "=" * ((4 - (len(ss) % 4)) % 4)
    return base64.b64decode(ss + pad)


def b64u_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("utf-8").rstrip("=")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--private-key", required=True, help="Ed25519 private key (b64url, 32 bytes). Keep secret.")
    ap.add_argument("--client-id", required=True, help="Your internal client identifier, e.g. CLINIC_A")
    ap.add_argument("--plan", required=True, type=int, choices=[1, 2, 3], help="1=1w trial, 2=1m trial, 3=1y license")
    ap.add_argument("--mode", default="BOTH", choices=["PORTAL", "SMS", "BOTH"], help="Product mode")
    ap.add_argument("--prefix", default="TSL1", help="Code prefix")
    args = ap.parse_args()

    issued_at = datetime.now(tz=timezone.utc)
    if args.plan == 1:
        expires_at = issued_at + timedelta(weeks=1)
    elif args.plan == 2:
        expires_at = issued_at + timedelta(days=30)
    else:
        expires_at = issued_at + timedelta(days=365)

    payload = {
        "v": 1,
        "client_id": args.client_id,
        "plan": args.plan,
        "mode": args.mode,
        "issued_at": issued_at.isoformat().replace("+00:00", "Z"),
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        "nonce": secrets.token_urlsafe(10),
    }

    payload_raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    payload_b64 = b64u_encode(payload_raw)

    priv_raw = b64u_decode(args.private_key)
    if len(priv_raw) != 32:
        raise SystemExit("Private key must decode to 32 bytes (Ed25519 raw private key)")
    priv = Ed25519PrivateKey.from_private_bytes(priv_raw)
    sig = priv.sign(payload_b64.encode("utf-8"))
    sig_b64 = b64u_encode(sig)

    print(f"{args.prefix}.{payload_b64}.{sig_b64}")


if __name__ == "__main__":
    main()
