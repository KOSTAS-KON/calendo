# sms/tools/run_outbox_once.py
from __future__ import annotations
import json, os, re, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTBOX_JSONL = PROJECT_ROOT / "data" / "output" / "outbox.jsonl"
PROVIDER_DEBUG_JSONL = PROJECT_ROOT / "data" / "output" / "provider_debug.jsonl"

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()

def parse_iso_any(s: Any) -> Optional[datetime]:
    if not s:
        return None
    t = str(s).strip()
    if not t:
        return None
    try:
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def to_e164_heuristic(phone: str, default_cc: str = "30") -> str:
    p = str(phone or "").strip()
    p = re.sub(r"[^\d+]", "", p)
    if not p:
        return ""
    if p.startswith("00"):
        return "+" + p[2:]
    if p.startswith("+"):
        return p
    digits = re.sub(r"\D", "", p)
    if len(digits) == 10 and digits.startswith("69"):
        return f"+{default_cc}{digits}"
    if digits.startswith(default_cc):
        return f"+{digits}"
    return p

def provider_log(payload: Dict[str, Any]) -> None:
    PROVIDER_DEBUG_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with PROVIDER_DEBUG_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

def normalize_infobip_base_url(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if not s.startswith("http://") and not s.startswith("https://"):
        s = "https://" + s
    return s.rstrip("/")

def effective_provider() -> str:
    prov = (os.getenv("SMS_PROVIDER") or "").strip().lower()
    if prov in ("mock", "infobip"):
        return prov
    base = normalize_infobip_base_url(os.getenv("INFOBIP_BASE_URL") or "")
    key = (os.getenv("INFOBIP_API_KEY") or "").strip()
    frm = (os.getenv("INFOBIP_FROM") or "").strip()
    if base and key and frm:
        return "infobip"
    return "mock"

def send_sms_infobip(to_e164: str, body: str) -> Tuple[str, str]:
    base = normalize_infobip_base_url(os.getenv("INFOBIP_BASE_URL") or "")
    key = (os.getenv("INFOBIP_API_KEY") or "").strip()
    frm = (os.getenv("INFOBIP_FROM") or "").strip()
    if not (base and key and frm):
        raise RuntimeError("Infobip not configured (INFOBIP_BASE_URL/API_KEY/FROM missing)")

    url = f"{base}/sms/2/text/advanced"
    headers = {"Authorization": f"App {key}", "Content-Type": "application/json", "Accept": "application/json"}
    payload = {"messages": [{"from": frm, "destinations": [{"to": to_e164}], "text": body}]}
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Infobip HTTP {r.status_code}: {(r.text or '')[:800]}")

    msgid = ""
    status_name = ""
    try:
        j = r.json()
        msg = j["messages"][0]
        msgid = str(msg.get("messageId") or "")
        status_name = str((msg.get("status") or {}).get("name") or "")
    except Exception:
        pass
    if not msgid:
        msgid = "infobip_" + uuid.uuid4().hex[:12]
    return msgid, status_name

def send_sms_mock(to_e164: str, body: str) -> Tuple[str, str]:
    msgid = "mock_" + uuid.uuid4().hex[:12]
    provider_log({"ts": iso_utc(now_utc()), "provider": "mock", "to": to_e164, "body": body, "msgid": msgid})
    return msgid, "MOCK"

def outbox_read_state() -> Dict[str, Dict[str, Any]]:
    OUTBOX_JSONL.parent.mkdir(parents=True, exist_ok=True)
    if not OUTBOX_JSONL.exists():
        OUTBOX_JSONL.write_text("", encoding="utf-8")
    latest: Dict[str, Dict[str, Any]] = {}
    for line in OUTBOX_JSONL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        oid = str(ev.get("outbox_id") or "").strip()
        if not oid:
            continue
        merged = dict(latest.get(oid, {}))
        merged.update(ev)
        latest[oid] = merged
    return latest

def outbox_append(ev: Dict[str, Any]) -> None:
    OUTBOX_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with OUTBOX_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")

def process_due(limit: int = 200) -> Tuple[int, int, int]:
    state = outbox_read_state()
    now = now_utc()
    due = []
    for oid, r in state.items():
        if str(r.get("status")) != "queued":
            continue
        sched = parse_iso_any(r.get("scheduled_for_iso"))
        if not sched:
            continue
        if sched.astimezone(timezone.utc) <= now + timedelta(seconds=2):
            due.append((oid, r))
        if len(due) >= limit:
            break

    sent = failed = 0
    for oid, r in due:
        to_ = to_e164_heuristic(r.get("to", ""))
        body = str(r.get("body") or "")
        try:
            prov = effective_provider()
            if prov == "infobip":
                mid, pst = send_sms_infobip(to_, body)
            else:
                mid, pst = send_sms_mock(to_, body)
            outbox_append({"outbox_id": oid, "ts_iso": iso_utc(now_utc()), "status": "sent",
                          "provider": prov, "provider_msgid": mid, "provider_status": pst, "error": ""})
            sent += 1
        except Exception as e:
            outbox_append({"outbox_id": oid, "ts_iso": iso_utc(now_utc()), "status": "failed",
                          "provider": effective_provider(), "error": str(e)})
            failed += 1

    return sent, failed, len(due)

if __name__ == "__main__":
    sent, failed, due = process_due()
    print(f"OUTBOX: due={due} sent={sent} failed={failed} provider={effective_provider()}")
