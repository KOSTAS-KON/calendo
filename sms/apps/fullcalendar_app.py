# -*- coding: utf-8 -*-
"""
Calendo SMS Calendar (Streamlit) — Restored + Move/Cancel SMS + One-click Send Now (Immediate)

Added:
- ✅ One-click buttons immediately SEND (enqueue + process_due_outbox)
- ✅ Outbox can delete queued messages
- ✅ Outbox shows reminder types (24h / 2h) naturally
- ✅ Fix: outbox_state_df always has columns to avoid KeyError
"""

from __future__ import annotations

import json
import os
import re
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st

try:
    from streamlit_calendar import calendar as st_calendar
except Exception:
    st_calendar = None

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired  # type: ignore


# =============================================================================
# SSO
# =============================================================================
def _get_query_params() -> dict:
    try:
        return dict(st.query_params)
    except Exception:
        try:
            return {k: v[0] if isinstance(v, list) and v else v for k, v in (st.experimental_get_query_params() or {}).items()}
        except Exception:
            return {}


def _require_sso() -> tuple[str, bool]:
    qp = _get_query_params()

    tenant = qp.get("tenant") or "default"
    if isinstance(tenant, list):
        tenant = tenant[0] if tenant else "default"
    tenant = str(tenant).strip().lower() or "default"
    os.environ["TENANT_SLUG"] = tenant

    sso = qp.get("sso") or ""
    if isinstance(sso, list):
        sso = sso[0] if sso else ""
    sso = str(sso).strip()

    if st.session_state.get("_sso_ok") and st.session_state.get("_tenant") == tenant:
        return tenant, True

    secret = (os.getenv("SSO_SHARED_SECRET") or os.getenv("SESSION_SECRET") or os.getenv("SECRET_KEY") or "").strip()
    if not secret:
        st.error("Security is not configured: missing SSO_SHARED_SECRET. Please set it in the environment.")
        st.stop()

    max_age = int(os.getenv("SSO_MAX_AGE_SECONDS") or "900")
    ser = URLSafeTimedSerializer(secret_key=secret, salt="calendo-sms-sso-v1")

    if not sso:
        st.warning("Please open the SMS Calendar from the Therapy Portal (missing access token).")
        st.stop()

    try:
        payload = ser.loads(sso, max_age=max_age)
    except SignatureExpired:
        st.warning("Your access link has expired. Please open the SMS Calendar again from the Therapy Portal.")
        st.stop()
    except BadSignature:
        st.error("Invalid access token. Please open the SMS Calendar again from the Therapy Portal.")
        st.stop()
    except Exception:
        st.error("Access token validation failed. Please try again from the Therapy Portal.")
        st.stop()

    token_tenant = str((payload or {}).get("tenant") or "").strip().lower()
    if token_tenant and token_tenant != tenant:
        st.error("Tenant mismatch. Please open the SMS Calendar again from the Therapy Portal.")
        st.stop()

    st.session_state["_sso_ok"] = True
    st.session_state["_tenant"] = tenant
    return tenant, True


TENANT_SLUG, _ = _require_sso()


# =============================================================================
# PATHS / SCHEMAS
# =============================================================================
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
CALENDAR_DIR = DATA_DIR / "calendar"
OUTPUT_DIR = DATA_DIR / "output"

APPOINTMENTS_CSV = CALENDAR_DIR / "appointments.csv"
CUSTOMERS_CSV = CALENDAR_DIR / "customers.csv"
TEMPLATES_JSON = CALENDAR_DIR / "templates.json"

OUTBOX_JSONL = OUTPUT_DIR / "outbox.jsonl"

APPT_HEADER = [
    "appointment_id",
    "customer_id",
    "customer_name",
    "customer_phone",
    "start_iso",
    "end_iso",
    "status",
    "service",
    "notes",
    "created_at_iso",
    "updated_at_iso",
    "pref_send_new_now",
    "pref_reminder_day",
    "pref_reminder_2h",
    "pref_send_moved_now",
    "pref_send_cancel_now",
]

CUSTOMER_HEADER = [
    "customer_id",
    "name",
    "phone",
    "notes",
    "consent",
    "created_at_iso",
    "updated_at_iso",
]

DEFAULT_TEMPLATES = {
    "new": "New appointment for {name} on {date} at {time}.",
    "reminder_day": "Reminder: {name} has an appointment tomorrow ({date}) at {time}.",
    "reminder_2h": "Reminder: {name} has an appointment at {time} (in ~2 hours).",
    "moved": "Update: {name}'s appointment moved to {date} {time}.",
    "cancelled": "Cancelled: {name}'s appointment on {date} {time} was cancelled.",
}


# =============================================================================
# HELPERS
# =============================================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso_any(dt_s: Any) -> Optional[datetime]:
    if not dt_s:
        return None
    s = str(dt_s).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def get_app_tz() -> Any:
    tz_name = os.getenv("APP_TIMEZONE", "Europe/Athens")
    if ZoneInfo:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            return timezone.utc
    return timezone.utc


def to_local(dt: datetime, tz: Any) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def to_utc(dt: datetime, tz: Any) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def normalize_phone(phone: str) -> str:
    if not phone:
        return ""
    p = str(phone).strip()
    return re.sub(r"[^\d+]", "", p)


def to_e164_heuristic(phone: str, default_cc: str = "30") -> str:
    p = normalize_phone(phone)
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


def ensure_dirs() -> None:
    CALENDAR_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def ensure_csv(path: Path, header: List[str]) -> None:
    if path.exists():
        return
    pd.DataFrame(columns=header).to_csv(path, index=False, encoding="utf-8")


def read_csv_df(path: Path, header: List[str]) -> pd.DataFrame:
    ensure_csv(path, header)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for c in header:
        if c not in df.columns:
            df[c] = ""
    return df[header]


def write_csv_df(path: Path, df: pd.DataFrame, header: List[str]) -> None:
    for c in header:
        if c not in df.columns:
            df[c] = ""
    df[header].to_csv(path, index=False, encoding="utf-8")


def load_templates() -> Dict[str, str]:
    if not TEMPLATES_JSON.exists():
        TEMPLATES_JSON.write_text(json.dumps(DEFAULT_TEMPLATES, ensure_ascii=False, indent=2), encoding="utf-8")
        return dict(DEFAULT_TEMPLATES)
    try:
        data = json.loads(TEMPLATES_JSON.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return dict(DEFAULT_TEMPLATES)
        for k, v in DEFAULT_TEMPLATES.items():
            data.setdefault(k, v)
        return {str(k): str(v) for k, v in data.items()}
    except Exception:
        return dict(DEFAULT_TEMPLATES)


def save_templates(tpls: Dict[str, str]) -> None:
    TEMPLATES_JSON.write_text(json.dumps(tpls, ensure_ascii=False, indent=2), encoding="utf-8")


def render_tpl(tpl: str, name: str, start_local: datetime) -> str:
    return (
        tpl.replace("{name}", name or "")
        .replace("{date}", start_local.strftime("%Y-%m-%d"))
        .replace("{time}", start_local.strftime("%H:%M"))
    )


def _i01(v: Any, default: int = 0) -> int:
    if v is None or str(v).strip() == "":
        return default
    try:
        return 1 if int(v) == 1 else 0
    except Exception:
        s = str(v).strip().lower()
        return 1 if s in ("true", "yes", "on") else 0


# =============================================================================
# OUTBOX
# =============================================================================
OUTBOX_COLUMNS = [
    "outbox_id",
    "scheduled_for_iso",
    "appointment_id",
    "to",
    "message_type",
    "body",
    "status",
    "provider",
    "provider_msgid",
    "provider_status",
    "error",
    "dedupe_key",
]


def outbox_jsonl_ensure() -> None:
    ensure_dirs()
    if not OUTBOX_JSONL.exists():
        OUTBOX_JSONL.write_text("", encoding="utf-8")


def outbox_jsonl_append(obj: Dict[str, Any]) -> None:
    outbox_jsonl_ensure()
    with OUTBOX_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def outbox_state_df() -> pd.DataFrame:
    outbox_jsonl_ensure()
    latest: Dict[str, Dict[str, Any]] = {}
    with OUTBOX_JSONL.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            oid = str(ev.get("outbox_id") or "").strip()
            if not oid:
                continue
            prev = latest.get(oid, {})
            merged = dict(prev)
            merged.update(ev)
            latest[oid] = merged

    rows = []
    for oid, r in latest.items():
        rows.append(
            {
                "outbox_id": oid,
                "scheduled_for_iso": r.get("scheduled_for_iso", ""),
                "appointment_id": r.get("appointment_id", ""),
                "to": r.get("to", ""),
                "message_type": r.get("message_type", ""),
                "body": r.get("body", ""),
                "status": r.get("status", ""),
                "provider": r.get("provider", ""),
                "provider_msgid": r.get("provider_msgid", ""),
                "provider_status": r.get("provider_status", ""),
                "error": r.get("error", ""),
                "dedupe_key": r.get("dedupe_key", ""),
            }
        )

    if not rows:
        return pd.DataFrame(columns=OUTBOX_COLUMNS)

    df = pd.DataFrame(rows)
    for c in OUTBOX_COLUMNS:
        if c not in df.columns:
            df[c] = ""
    return df[OUTBOX_COLUMNS]


def _dedupe_key(appt_id: str, message_type: str, when_iso: str) -> str:
    return f"{appt_id}|{message_type}|{when_iso}"


def outbox_enqueue(appointment_id: str, to_phone: str, body: str, message_type: str, when_utc: datetime) -> bool:
    when_iso = iso_utc(when_utc)
    dk = _dedupe_key(appointment_id, message_type, when_iso)
    df = outbox_state_df()
    if not df.empty and (df["dedupe_key"] == dk).any():
        return False

    outbox_jsonl_append(
        {
            "outbox_id": str(uuid.uuid4()),
            "ts_iso": iso_utc(now_utc()),
            "scheduled_for_iso": when_iso,
            "appointment_id": appointment_id,
            "to": to_e164_heuristic(to_phone),
            "message_type": message_type,
            "body": body,
            "status": "queued",
            "provider": "",
            "provider_msgid": "",
            "provider_status": "",
            "error": "",
            "dedupe_key": dk,
        }
    )
    return True


def outbox_mark(outbox_id: str, *, status: str, provider: str, provider_msgid: str = "", provider_status: str = "", error: str = "") -> None:
    outbox_jsonl_append(
        {
            "outbox_id": outbox_id,
            "ts_iso": iso_utc(now_utc()),
            "status": status,
            "provider": provider,
            "provider_msgid": provider_msgid,
            "provider_status": provider_status,
            "error": error,
        }
    )


def outbox_delete(outbox_id: str) -> None:
    outbox_mark(outbox_id, status="deleted", provider="user", error="deleted_by_user")


def outbox_delete_future_for_appt(appointment_id: str, message_types: Optional[List[str]] = None) -> int:
    df = outbox_state_df()
    if df.empty:
        return 0
    now = now_utc()
    q = df[df["status"] == "queued"].copy()
    q = q[q["appointment_id"].astype(str) == str(appointment_id)]
    if message_types:
        q = q[q["message_type"].isin(message_types)]

    def _is_future(s: str) -> bool:
        dt = parse_iso_any(s)
        return bool(dt and dt.astimezone(timezone.utc) > now)

    q = q[q["scheduled_for_iso"].apply(_is_future)]
    count = 0
    for oid in q["outbox_id"].tolist():
        outbox_mark(str(oid), status="deleted", provider="system", error="superseded")
        count += 1
    return count


# =============================================================================
# Provider
# =============================================================================
def _normalize_infobip_base_url(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if not s.startswith("http://") and not s.startswith("https://"):
        s = "https://" + s
    return s.rstrip("/")


def _effective_provider() -> str:
    prov = (os.getenv("SMS_PROVIDER") or "").strip().lower()
    if prov in ("mock", "infobip"):
        return prov

    base = _normalize_infobip_base_url(os.getenv("INFOBIP_BASE_URL") or "")
    key = (os.getenv("INFOBIP_API_KEY") or "").strip()
    frm = (os.getenv("INFOBIP_FROM") or "").strip()
    if base and key and frm:
        return "infobip"
    return "mock"


def _send_sms_infobip(to_e164: str, body: str) -> Tuple[str, str]:
    base = _normalize_infobip_base_url(os.getenv("INFOBIP_BASE_URL") or "")
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
    stname = ""
    try:
        j = r.json()
        msg = j["messages"][0]
        msgid = str(msg.get("messageId") or "")
        stname = str((msg.get("status") or {}).get("name") or "")
    except Exception:
        pass
    if not msgid:
        msgid = "infobip_" + uuid.uuid4().hex[:12]
    return msgid, stname


def _send_sms_mock(to_e164: str, body: str) -> Tuple[str, str]:
    msgid = "mock_" + uuid.uuid4().hex[:12]
    return msgid, "MOCK"


def _send_via_provider(to_e164: str, body: str) -> Tuple[str, str, str]:
    prov = _effective_provider()
    if prov == "infobip":
        mid, pst = _send_sms_infobip(to_e164, body)
        return prov, mid, pst
    mid, pst = _send_sms_mock(to_e164, body)
    return "mock", mid, pst


def process_due_outbox(limit: int = 200) -> Tuple[int, int, int]:
    df = outbox_state_df()
    if df.empty:
        return (0, 0, 0)

    now = now_utc()
    due = df[df["status"] == "queued"].copy()

    def _is_due(s: str) -> bool:
        dt = parse_iso_any(s)
        return bool(dt and dt.astimezone(timezone.utc) <= now + timedelta(seconds=2))

    due = due[due["scheduled_for_iso"].apply(_is_due)]
    if due.empty:
        return (0, 0, 0)

    due = due.head(limit)
    sent = failed = 0
    for _, r in due.iterrows():
        oid = str(r["outbox_id"])
        to_ = str(r["to"])
        body = str(r["body"])
        try:
            prov, mid, pst = _send_via_provider(to_, body)
            outbox_mark(oid, status="sent", provider=prov, provider_msgid=mid, provider_status=pst, error="")
            sent += 1
        except Exception as e:
            outbox_mark(oid, status="failed", provider=_effective_provider(), error=str(e))
            failed += 1
    return (sent, failed, len(due))


# =============================================================================
# Appointment ops
# =============================================================================
def _queue_reminders_for_appt(appt_row: Dict[str, str], templates: Dict[str, str], tz: Any) -> None:
    appt_id = appt_row["appointment_id"]
    start_utc = parse_iso_any(appt_row["start_iso"]) or now_utc()
    start_local = to_local(start_utc, tz)

    if _i01(appt_row.get("pref_reminder_day"), 0) == 1:
        when = start_utc - timedelta(hours=24)
        if when > now_utc():
            body = render_tpl(templates["reminder_day"], appt_row["customer_name"], start_local)
            outbox_enqueue(appt_id, appt_row["customer_phone"], body, "reminder_day", when)

    if _i01(appt_row.get("pref_reminder_2h"), 0) == 1:
        when = start_utc - timedelta(hours=2)
        if when > now_utc():
            body = render_tpl(templates["reminder_2h"], appt_row["customer_name"], start_local)
            outbox_enqueue(appt_id, appt_row["customer_phone"], body, "reminder_2h", when)


def _immediate_send_feedback() -> None:
    sent, failed, due = process_due_outbox(limit=50)
    if due == 0:
        st.info("Nothing due to send.")
    elif failed == 0:
        st.success(f"Sent {sent} SMS (provider={_effective_provider()}).")
    else:
        st.warning(f"Processed due={due}: sent={sent}, failed={failed}. Check Outbox for errors.")


def _send_now_buttons_for_appt(appt_row: Dict[str, str], templates: Dict[str, str], tz: Any) -> None:
    appt_id = appt_row["appointment_id"]
    start_utc = parse_iso_any(appt_row["start_iso"]) or now_utc()
    start_local = to_local(start_utc, tz)

    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("📨 Send NEW now", key=f"send_new_{appt_id}"):
            body = render_tpl(templates["new"], appt_row["customer_name"], start_local)
            outbox_enqueue(appt_id, appt_row["customer_phone"], body, "new", now_utc())
            _immediate_send_feedback()
    with c2:
        if st.button("🔁 Send MOVED now", key=f"send_moved_{appt_id}"):
            body = render_tpl(templates["moved"], appt_row["customer_name"], start_local)
            outbox_enqueue(appt_id, appt_row["customer_phone"], body, "moved", now_utc())
            _immediate_send_feedback()
    with c3:
        if st.button("❌ Send CANCELLED now", key=f"send_cancel_{appt_id}"):
            body = render_tpl(templates["cancelled"], appt_row["customer_name"], start_local)
            outbox_enqueue(appt_id, appt_row["customer_phone"], body, "cancelled", now_utc())
            _immediate_send_feedback()


def _move_appt(appt_id: str, new_start_iso: str, new_end_iso: str, templates: Dict[str, str], tz: Any) -> None:
    adf = read_csv_df(APPOINTMENTS_CSV, APPT_HEADER)
    rows = adf.index[adf["appointment_id"] == appt_id].tolist()
    if not rows:
        return
    i = rows[0]
    if adf.at[i, "status"] == "cancelled":
        return

    outbox_delete_future_for_appt(appt_id, message_types=["reminder_day", "reminder_2h"])

    adf.at[i, "start_iso"] = new_start_iso
    adf.at[i, "end_iso"] = new_end_iso
    adf.at[i, "updated_at_iso"] = iso_utc(now_utc())
    write_csv_df(APPOINTMENTS_CSV, adf, APPT_HEADER)

    appt_row = adf.loc[i].to_dict()
    start_utc = parse_iso_any(appt_row["start_iso"]) or now_utc()
    start_local = to_local(start_utc, tz)

    if _i01(appt_row.get("pref_send_moved_now"), 1) == 1:
        body = render_tpl(templates["moved"], appt_row["customer_name"], start_local)
        outbox_enqueue(appt_id, appt_row["customer_phone"], body, "moved", now_utc())

    _queue_reminders_for_appt(appt_row, templates, tz)


def _cancel_appt(appt_id: str, templates: Dict[str, str], tz: Any) -> None:
    adf = read_csv_df(APPOINTMENTS_CSV, APPT_HEADER)
    rows = adf.index[adf["appointment_id"] == appt_id].tolist()
    if not rows:
        return
    i = rows[0]
    if adf.at[i, "status"] == "cancelled":
        return

    adf.at[i, "status"] = "cancelled"
    adf.at[i, "updated_at_iso"] = iso_utc(now_utc())
    write_csv_df(APPOINTMENTS_CSV, adf, APPT_HEADER)

    outbox_delete_future_for_appt(appt_id, message_types=["reminder_day", "reminder_2h", "new", "moved"])

    appt_row = adf.loc[i].to_dict()
    start_utc = parse_iso_any(appt_row["start_iso"]) or now_utc()
    start_local = to_local(start_utc, tz)

    if _i01(appt_row.get("pref_send_cancel_now"), 1) == 1:
        body = render_tpl(templates["cancelled"], appt_row["customer_name"], start_local)
        outbox_enqueue(appt_id, appt_row["customer_phone"], body, "cancelled", now_utc())


# =============================================================================
# Pages
# =============================================================================
def page_appointments(templates: Dict[str, str], tz: Any) -> None:
    st.subheader("Appointments")
    adf = read_csv_df(APPOINTMENTS_CSV, APPT_HEADER)
    if adf.empty:
        st.info("No appointments.")
        return

    adf["_start_dt"] = adf["start_iso"].apply(parse_iso_any)
    adf = adf.sort_values(by="_start_dt", ascending=False).drop(columns=["_start_dt"], errors="ignore")

    st.dataframe(adf[["appointment_id", "customer_name", "customer_phone", "start_iso", "end_iso", "status", "service"]], width="stretch")

    appt_ids = adf["appointment_id"].tolist()
    appt_id = st.selectbox("Appointment", appt_ids, key="appt_select_main")
    row = adf[adf["appointment_id"] == appt_id].iloc[0].to_dict()

    st.markdown("### One-click live SMS (sends immediately)")
    _send_now_buttons_for_appt(row, templates, tz)

    st.markdown("### Manual move")
    new_start_local = st.datetime_input("New start (local)", value=datetime.now(tz).replace(second=0, microsecond=0) + timedelta(hours=2), key="move_new_start")
    new_duration = st.number_input("New duration (min)", min_value=15, max_value=240, value=45, step=15, key="move_new_dur")
    if st.button("Apply move", key="apply_move_btn"):
        new_start_utc = to_utc(new_start_local, tz)
        new_end_utc = new_start_utc + timedelta(minutes=int(new_duration))
        _move_appt(appt_id, iso_utc(new_start_utc), iso_utc(new_end_utc), templates, tz)
        st.success("Moved; SMS/reminders updated.")
        _immediate_send_feedback()
        st.rerun()

    st.markdown("### Cancel")
    if st.button("Cancel appointment", key="cancel_btn"):
        _cancel_appt(appt_id, templates, tz)
        st.success("Cancelled; SMS updated.")
        _immediate_send_feedback()
        st.rerun()


def page_outbox() -> None:
    st.subheader("Outbox")
    df = outbox_state_df()
    if df.empty:
        st.info("Outbox is empty.")
        return

    st.dataframe(df.sort_values(by="scheduled_for_iso", ascending=False), width="stretch")

    queued = df[df["status"] == "queued"].copy()
    st.markdown("### Delete queued message")
    if queued.empty:
        st.info("No queued messages to delete.")
        return

    choices = [f"{r['outbox_id']} | {r['message_type']} | {r['scheduled_for_iso']} | {r['to']}" for _, r in queued.iterrows()]
    pick = st.selectbox("Queued message", choices, key="outbox_delete_pick")
    if st.button("🗑 Delete selected", key="outbox_delete_btn"):
        oid = pick.split("|")[0].strip()
        outbox_delete(oid)
        st.success("Deleted (marked).")
        st.rerun()


# =============================================================================
# MAIN
# =============================================================================
def main() -> None:
    st.set_page_config(page_title="Calendo SMS", layout="wide")
    tz = get_app_tz()
    templates = load_templates()

    ensure_dirs()
    ensure_csv(CUSTOMERS_CSV, CUSTOMER_HEADER)
    ensure_csv(APPOINTMENTS_CSV, APPT_HEADER)
    outbox_jsonl_ensure()

    tabs = st.tabs(["📋 Appointments", "📨 Outbox"])
    with tabs[0]:
        page_appointments(templates, tz)
    with tabs[1]:
        page_outbox()


if __name__ == "__main__":
    main()
