# -*- coding: utf-8 -*-
"""
sms3 - bulletproof outbox sender

Reads:   data/output/outbox.csv
Writes:  data/output/outbox.csv
Debug:   data/output/provider_debug.jsonl  (append JSONL on every send attempt)

Run:
  python -m src.calendar.send_outbox
"""

from __future__ import annotations

import csv
import json
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from src.providers.infobip_client_http import InfobipHttpClient


PROJECT_ROOT = Path(__file__).resolve().parents[2]  # .../sms3
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = DATA_DIR / "output"
CALENDAR_DIR = DATA_DIR / "calendar"

OUTBOX_PRIMARY = OUTPUT_DIR / "outbox.csv"
OUTBOX_FALLBACK = CALENDAR_DIR / "outbox.csv"
PROVIDER_DEBUG_PATH = OUTPUT_DIR / "provider_debug.jsonl"

DEFAULT_SENDER = os.getenv("SMS_SENDER", "sms3")
DEFAULT_PROVIDER = os.getenv("SMS_PROVIDER", "infobip").strip().lower()

MAX_SEND_PER_RUN = int(os.getenv("SMS_MAX_PER_RUN", "50"))
RETRY_ATTEMPTS = int(os.getenv("SMS_RETRY_ATTEMPTS", "2"))
RETRY_BACKOFF_SEC = float(os.getenv("SMS_RETRY_BACKOFF_SEC", "1.5"))

REQUIRE_CONFIRMED = os.getenv("SMS_REQUIRE_CONFIRMED", "1").strip() != "0"

OUTBOX_HEADER = [
    "outbox_id",
    "ts_iso",
    "scheduled_for_iso",
    "to",
    "customer_id",
    "appointment_id",
    "message_type",
    "text",
    "status",
    "confirmed",
    "provider",
    "provider_status",
    "provider_message_id",
    "http_status",
    "error",
    "dedupe_key",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def safe_int01(x: Any) -> int:
    if x in (None, "", "nan", "NaN"):
        return 0
    try:
        return 1 if int(float(str(x).strip())) == 1 else 0
    except Exception:
        return 0


def ensure_outbox_exists(path: Path) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CALENDAR_DIR.mkdir(parents=True, exist_ok=True)

    if path.exists():
        return path

    if OUTBOX_FALLBACK.exists():
        try:
            OUTBOX_PRIMARY.write_text(OUTBOX_FALLBACK.read_text(encoding="utf-8"), encoding="utf-8")
            return OUTBOX_PRIMARY
        except Exception:
            pass

    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTBOX_HEADER)
        w.writeheader()
    return path


def read_outbox() -> pd.DataFrame:
    path = ensure_outbox_exists(OUTBOX_PRIMARY)
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    except Exception:
        df = pd.DataFrame(columns=OUTBOX_HEADER)
    for c in OUTBOX_HEADER:
        if c not in df.columns:
            df[c] = ""
    return df[OUTBOX_HEADER].copy()


def write_outbox(df: pd.DataFrame) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    df2 = df.copy()
    for c in OUTBOX_HEADER:
        if c not in df2.columns:
            df2[c] = ""
    df2 = df2[OUTBOX_HEADER]
    df2.to_csv(OUTBOX_PRIMARY, index=False, encoding="utf-8")


def append_provider_debug(obj: Dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with PROVIDER_DEBUG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception:
        # never crash sender due to debug logging
        pass


def promote_scheduled_to_queued(df: pd.DataFrame, now: datetime) -> Tuple[pd.DataFrame, int]:
    promoted = 0
    for idx, r in df.iterrows():
        if str(r.get("status", "")).strip().lower() != "scheduled":
            continue
        sched = parse_dt_iso(str(r.get("scheduled_for_iso", "")).strip())
        if sched and sched <= now:
            df.at[idx, "status"] = "queued"
            df.at[idx, "scheduled_for_iso"] = ""
            promoted += 1
    return df, promoted


def init_client() -> InfobipHttpClient:
    return InfobipHttpClient.from_secrets()


def extract_provider_message_id(resp_json: Any) -> str:
    try:
        return InfobipHttpClient.extract_message_id(resp_json)
    except Exception:
        return ""


def extract_provider_status(resp_json: Any) -> str:
    try:
        msgs = (resp_json or {}).get("messages") or []
        if msgs and isinstance(msgs, list):
            stt = msgs[0].get("status") or {}
            return str(stt.get("name") or stt.get("groupName") or "")
    except Exception:
        return ""
    return ""


def send_via_provider(to: str, text: str) -> Tuple[Optional[int], str, str, str]:
    sender = os.getenv("INFOBIP_SENDER", DEFAULT_SENDER)
    client = init_client()
    resp = client.send_sms_advanced(to=str(to), sender=str(sender), text=str(text))
    status_code = int(resp.get("status_code")) if resp.get("status_code") not in (None, "") else None
    body = resp.get("body") or ""
    resp_json = resp.get("json")
    msg_id = extract_provider_message_id(resp_json)
    prov_status = extract_provider_status(resp_json)
    return status_code, body, msg_id, prov_status


def send_outbox() -> Dict[str, int]:
    now = utcnow()
    df = read_outbox()

    df["status"] = df["status"].astype(str)
    df["confirmed"] = df["confirmed"].astype(str)

    df, promoted = promote_scheduled_to_queued(df, now)

    queued_mask = df["status"].str.lower().eq("queued")

    if REQUIRE_CONFIRMED:
        confirmed_mask = df["confirmed"].apply(safe_int01).astype(bool)
        blocked = int((queued_mask & ~confirmed_mask).sum())
        send_candidates = df[queued_mask & confirmed_mask].copy()
    else:
        blocked = 0
        send_candidates = df[queued_mask].copy()

    send_candidates = send_candidates.head(MAX_SEND_PER_RUN).copy()

    sent_ok = 0
    sent_failed = 0

    for _, row in send_candidates.iterrows():
        outbox_id = str(row.get("outbox_id", "")).strip()
        to = str(row.get("to", "")).strip()
        text = str(row.get("text", "")).strip()
        appt_id = str(row.get("appointment_id", "")).strip()

        attempt_debug: Dict[str, Any] = {
            "ts_iso": iso_utc(utcnow()),
            "outbox_id": outbox_id,
            "appointment_id": appt_id,
            "to": to,
            "http_status": None,
            "response_body": "",
            "error_name": None,
            "error_description": None,
        }

        ok = False
        last_err_name = ""
        last_err_desc = ""
        last_http_status: Optional[int] = None
        last_body = ""
        last_msg_id = ""
        last_provider_status = ""

        for attempt in range(RETRY_ATTEMPTS + 1):
            try:
                http_status, body, msg_id, prov_status = send_via_provider(to=to, text=text)
                last_http_status = http_status
                last_body = body or ""
                last_msg_id = msg_id or ""
                last_provider_status = prov_status or ""

                if http_status is not None and 200 <= int(http_status) < 300:
                    ok = True
                    break
                last_err_name = "ProviderHTTPError"
                last_err_desc = f"http_status={http_status}"
            except Exception as e:
                last_err_name = type(e).__name__
                last_err_desc = str(e)
                last_body = traceback.format_exc()

            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SEC * (attempt + 1))

        attempt_debug["http_status"] = last_http_status
        attempt_debug["response_body"] = last_body
        attempt_debug["error_name"] = None if ok else last_err_name
        attempt_debug["error_description"] = None if ok else last_err_desc
        append_provider_debug(attempt_debug)

        row_index = df.index[df["outbox_id"].astype(str) == outbox_id]
        if len(row_index) == 0:
            continue
        i = row_index[0]

        df.at[i, "provider"] = DEFAULT_PROVIDER
        df.at[i, "http_status"] = "" if last_http_status is None else str(last_http_status)
        df.at[i, "provider_message_id"] = last_msg_id
        df.at[i, "provider_status"] = last_provider_status

        if ok:
            df.at[i, "status"] = "sent"
            df.at[i, "error"] = ""
            sent_ok += 1
        else:
            df.at[i, "status"] = "failed"
            df.at[i, "error"] = last_err_name
            sent_failed += 1

    write_outbox(df)

    return {
        "promoted": promoted,
        "blocked_queued_unconfirmed": blocked,
        "queued_sent": sent_ok,
        "queued_failed": sent_failed,
    }


if __name__ == "__main__":
    print(">>> send_outbox.py EXECUTED <<<")
    print(send_outbox())
