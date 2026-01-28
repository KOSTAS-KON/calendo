from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timedelta

import requests
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.sms_outbox import SmsOutbox
from app.models.clinic_settings import ClinicSettings
from app.models.tenant import Tenant


MAX_ATTEMPTS = int(os.getenv("SMS_MAX_ATTEMPTS", "3"))
BACKOFF_SECONDS = int(os.getenv("SMS_BACKOFF_SECONDS", "120"))  # 2 minutes


def _utcnow() -> datetime:
    return datetime.utcnow()


def _infobip_send(base_url: str, api_key: str, sender: str, to_phone: str, message: str) -> tuple[bool, str, str | None]:
    """Send SMS using Infobip HTTP API. Returns (ok, error, provider_message_id)."""
    base_url = base_url.rstrip("/")
    url = f"{base_url}/sms/2/text/advanced"

    headers = {
        "Authorization": f"App {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "messages": [
            {
                "from": sender,
                "destinations": [{"to": to_phone}],
                "text": message,
            }
        ]
    }

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=20)
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}: {r.text[:500]}", None

        data = r.json()
        # Best-effort extract messageId
        msg_id = None
        try:
            msg_id = data.get("messages", [{}])[0].get("messageId")
        except Exception:
            msg_id = None
        return True, "", msg_id
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", None


def _get_tenant_settings(db: Session, tenant_id: str) -> tuple[str | None, str | None, str | None]:
    cs = db.query(ClinicSettings).filter(ClinicSettings.tenant_id == tenant_id).first()
    if not cs:
        return None, None, None
    base_url = (getattr(cs, "infobip_base_url", "") or getattr(cs, "sms_base_url", "") or "").strip()
    api_key = (getattr(cs, "infobip_api_key", "") or "").strip()
    sender = (getattr(cs, "infobip_sender", "") or "").strip()
    return base_url or None, api_key or None, sender or None


def process_due(db: Session) -> tuple[int, int, int]:
    now = _utcnow()
    due = (
        db.query(SmsOutbox)
        .filter(SmsOutbox.status.in_(["queued", "retry"]))
        .filter(SmsOutbox.scheduled_at <= now)
        .order_by(SmsOutbox.scheduled_at.asc())
        .limit(25)
        .all()
    )

    seen = len(due)
    sent = 0
    failed = 0

    for item in due:
        # move to sending
        item.status = "sending"
        db.add(item)
    db.commit()

    for item in due:
        base_url, api_key, sender = _get_tenant_settings(db, item.tenant_id)

        if not base_url or not api_key or not sender:
            item.attempts = int(item.attempts or 0) + 1
            item.last_error = "Missing Infobip credentials in clinic settings"
            if item.attempts >= MAX_ATTEMPTS:
                item.status = "failed"
                failed += 1
            else:
                item.status = "retry"
                item.scheduled_at = now + timedelta(seconds=BACKOFF_SECONDS)
            db.add(item)
            db.commit()
            continue

        ok, err, msg_id = _infobip_send(base_url, api_key, sender, item.to_phone, item.message)
        if ok:
            item.status = "sent"
            item.provider_message_id = msg_id
            item.last_error = None
            db.add(item)
            db.commit()
            sent += 1
        else:
            item.attempts = int(item.attempts or 0) + 1
            item.last_error = err
            if item.attempts >= MAX_ATTEMPTS:
                item.status = "failed"
                failed += 1
            else:
                item.status = "retry"
                item.scheduled_at = _utcnow() + timedelta(seconds=BACKOFF_SECONDS)
            db.add(item)
            db.commit()

    return seen, sent, failed


def run_once():
    db = SessionLocal()
    try:
        seen, sent, failed = process_due(db)
        print(f"SMS_WORKER: seen={seen} sent={sent} failed={failed}")
    finally:
        db.close()


def run_forever():
    interval = int(os.getenv("SMS_WORKER_INTERVAL", "30"))
    print("SMS_WORKER: starting loop")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"SMS_WORKER: error {type(e).__name__}: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    once = os.getenv("SMS_WORKER_ONCE", "").strip() in ("1", "true", "yes")
    if once:
        run_once()
    else:
        run_forever()
