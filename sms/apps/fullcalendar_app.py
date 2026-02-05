# -*- coding: utf-8 -*-
"""
Calendo SMS Calendar (Streamlit) — RESTORED FEATURES

Restored:
- ✅ Send SMS NOW (one click) per appointment + per message type (NEW/MOVED/CANCELLED/REMINDERS)
- ✅ Reminder scheduling: 24h before + 2h before (opt-in per appointment at creation)
- ✅ Calendar view selector: Month / Week / Day (FullCalendar initialView)
- ✅ Outbox queue + “Process due now” (sends queued messages using provider)
- ✅ Tenant-aware Portal fetch: /api/internal/clinic_settings?tenant=...
- ✅ Tenant-aware license check: /api/license?tenant=... (with X-Internal-Key)
- ✅ SSO token required

Notes:
- Data stored in data/calendar/*.csv and data/output/outbox.jsonl
- Provider supports: mock + infobip (if env creds exist)
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
            return {
                k: v[0] if isinstance(v, list) and v else v
                for k, v in (st.experimental_get_query_params() or {}).items()
            }
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
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # repo root (sms/apps/.. -> repo)
DATA_DIR = PROJECT_ROOT / "data"
CALENDAR_DIR = DATA_DIR / "calendar"
OUTPUT_DIR = DATA_DIR / "output"

APPOINTMENTS_CSV = CALENDAR_DIR / "appointments.csv"
CUSTOMERS_CSV = CALENDAR_DIR / "customers.csv"
TEMPLATES_JSON = CALENDAR_DIR / "templates.json"

OUTBOX_JSONL = OUTPUT_DIR / "outbox.jsonl"
PROVIDER_DEBUG_JSONL = OUTPUT_DIR / "provider_debug.jsonl"

APPT_HEADER = [
    "appointment_id",
    "customer_id",
    "customer_name",
    "customer_phone",
    "start_iso",  # UTC ISO
    "end_iso",    # UTC ISO
    "status",     # active|cancelled
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
# TIME / PHONE / CSV HELPERS
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
# OUTBOX (queue + sending)
# =============================================================================
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
    return pd.DataFrame(rows)


def _dedupe_key(appt_id: str, message_type: str, when_iso: str) -> str:
    return f"{appt_id}|{message_type}|{when_iso}"


def outbox_enqueue(appointment_id: str, to_phone: str, body: str, message_type: str, when_utc: datetime) -> bool:
    outbox_jsonl_ensure()
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


# =============================================================================
# Provider (mock + Infobip)
# =============================================================================
def provider_log(payload: Dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with PROVIDER_DEBUG_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


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


def _send_sms_mock(to_e164: str, body: str) -> Tuple[str, str]:
    msgid = "mock_" + uuid.uuid4().hex[:12]
    provider_log({"ts": iso_utc(now_utc()), "provider": "mock", "to": to_e164, "body": body, "msgid": msgid})
    return msgid, "MOCK"


def _send_sms_infobip(to_e164: str, body: str) -> Tuple[str, str]:
    base = _normalize_infobip_base_url(os.getenv("INFOBIP_BASE_URL") or "")
    key = (os.getenv("INFOBIP_API_KEY") or "").strip()
    frm = (os.getenv("INFOBIP_FROM") or "").strip()

    if not (base and key and frm):
        raise RuntimeError("Infobip not configured. Set INFOBIP_BASE_URL, INFOBIP_API_KEY, INFOBIP_FROM")

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
        stobj = msg.get("status") or {}
        stname = str(stobj.get("name") or "")
    except Exception:
        pass
    if not msgid:
        msgid = "infobip_" + uuid.uuid4().hex[:12]
    return msgid, stname


def _send_via_provider(to_e164: str, body: str) -> Tuple[str, str, str]:
    prov = _effective_provider()
    if prov == "mock":
        mid, pst = _send_sms_mock(to_e164, body)
        return prov, mid, pst
    mid, pst = _send_sms_infobip(to_e164, body)
    return "infobip", mid, pst


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
# Portal fetch (tenant-aware) + license
# =============================================================================
def _portal_base_url() -> str:
    for k in ("PORTAL_APP_URL", "PORTAL_BASE_URL", "THERAPY_PORTAL_URL"):
        v = (os.getenv(k) or "").strip().rstrip("/")
        if v:
            return v
    return ""


def _fetch_portal_json(path: str, headers: Dict[str, str] | None = None) -> Dict[str, Any] | None:
    base = _portal_base_url()
    if not base:
        return None
    url = base + path
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except Exception:
        return None


def _apply_portal_settings_to_env() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    tenant_slug = (os.getenv("TENANT_SLUG") or TENANT_SLUG or "default").strip().lower()
    internal_key = (os.getenv("INTERNAL_API_KEY") or "").strip()
    if not internal_key:
        return out

    data = _fetch_portal_json(f"/api/internal/clinic_settings?tenant={tenant_slug}", headers={"X-Internal-Key": internal_key}) or {}
    if isinstance(data, dict):
        out["clinic_name"] = data.get("clinic_name") or ""
        # If portal returns Infobip creds, set them:
        if data.get("infobip_base_url"):
            os.environ["INFOBIP_BASE_URL"] = str(data.get("infobip_base_url"))
        if data.get("infobip_api_key"):
            os.environ["INFOBIP_API_KEY"] = str(data.get("infobip_api_key"))
        if data.get("infobip_sender"):
            os.environ["INFOBIP_FROM"] = str(data.get("infobip_sender"))
    return out


# =============================================================================
# Bootstrap
# =============================================================================
def bootstrap() -> None:
    ensure_dirs()
    ensure_csv(CUSTOMERS_CSV, CUSTOMER_HEADER)
    ensure_csv(APPOINTMENTS_CSV, APPT_HEADER)
    outbox_jsonl_ensure()
    load_templates()


# =============================================================================
# Pages
# =============================================================================
def page_calendar(templates: Dict[str, str], tz: Any) -> None:
    st.subheader("Calendar")

    adf = read_csv_df(APPOINTMENTS_CSV, APPT_HEADER)
    cdf = read_csv_df(CUSTOMERS_CSV, CUSTOMER_HEADER)

    # Calendar view selector
    view = st.selectbox(
        "View",
        options=[("Month", "dayGridMonth"), ("Week", "timeGridWeek"), ("Day", "timeGridDay")],
        format_func=lambda x: x[0],
        key="cal_view",
    )[1]

    with st.expander("➕ New appointment", expanded=False):
        if cdf.empty:
            st.warning("No customers found.")
        else:
            customer_options = {f"{r['name']} ({r['phone']})": r for _, r in cdf.iterrows()}
            choice = st.selectbox("Customer", list(customer_options.keys()), key="appt_customer_select")
            service = st.text_input("Service", value="Session", key="appt_service")
            start_local = st.datetime_input(
                "Start (local)",
                value=datetime.now(tz).replace(second=0, microsecond=0) + timedelta(hours=1),
                key="appt_start_local",
            )
            duration_min = st.number_input("Duration (min)", min_value=15, max_value=240, value=45, step=15, key="appt_duration")
            notes = st.text_area("Notes", value="", key="appt_notes")

            c1, c2, c3 = st.columns(3)
            with c1:
                pref_send_now = st.checkbox("Send SMS now", value=True, key="pref_send_now")
            with c2:
                pref_day = st.checkbox("Reminder 24h before", value=False, key="pref_day")
            with c3:
                pref_2h = st.checkbox("Reminder 2h before", value=False, key="pref_2h")

            if st.button("Create appointment", key="appt_create_btn"):
                cust = customer_options[choice]
                start_utc = to_utc(start_local, tz)
                end_utc = start_utc + timedelta(minutes=int(duration_min))
                ts = iso_utc(now_utc())
                appt_id = str(uuid.uuid4())

                row = {
                    "appointment_id": appt_id,
                    "customer_id": cust["customer_id"],
                    "customer_name": cust["name"],
                    "customer_phone": cust["phone"],
                    "start_iso": iso_utc(start_utc),
                    "end_iso": iso_utc(end_utc),
                    "status": "active",
                    "service": service,
                    "notes": notes,
                    "created_at_iso": ts,
                    "updated_at_iso": ts,
                    "pref_send_new_now": "1" if pref_send_now else "0",
                    "pref_reminder_day": "1" if pref_day else "0",
                    "pref_reminder_2h": "1" if pref_2h else "0",
                    "pref_send_moved_now": "1",
                    "pref_send_cancel_now": "1",
                }

                adf = pd.concat([adf, pd.DataFrame([row])], ignore_index=True)
                write_csv_df(APPOINTMENTS_CSV, adf, APPT_HEADER)

                # NEW now
                if pref_send_now:
                    body = render_tpl(templates["new"], cust["name"], start_local)
                    outbox_enqueue(appt_id, cust["phone"], body, "new", now_utc())

                # reminders
                if pref_day:
                    when = start_utc - timedelta(hours=24)
                    if when > now_utc():
                        body = render_tpl(templates["reminder_day"], cust["name"], start_local)
                        outbox_enqueue(appt_id, cust["phone"], body, "reminder_day", when)

                if pref_2h:
                    when = start_utc - timedelta(hours=2)
                    if when > now_utc():
                        body = render_tpl(templates["reminder_2h"], cust["name"], start_local)
                        outbox_enqueue(appt_id, cust["phone"], body, "reminder_2h", when)

                st.success("Appointment created. SMS/reminders queued.")
                st.rerun()

    # Calendar render
    if st_calendar is not None:
        events = []
        for _, r in adf.iterrows():
            st_dt = parse_iso_any(r["start_iso"])
            en_dt = parse_iso_any(r["end_iso"])
            if not st_dt or not en_dt:
                continue
            title = f"{r['customer_name']} • {r['service']}"
            if r.get("status") == "cancelled":
                title = "❌ " + title
            events.append({"id": r["appointment_id"], "title": title, "start": st_dt.isoformat(), "end": en_dt.isoformat()})

        st_calendar(
            {
                "initialView": view,
                "height": 720,
                "headerToolbar": {"left": "prev,next today", "center": "title", "right": "dayGridMonth,timeGridWeek,timeGridDay"},
                "events": events,
            }
        )
    else:
        st.info("streamlit_calendar not installed; showing list instead.")
        page_appointments(templates, tz)


def page_appointments(templates: Dict[str, str], tz: Any) -> None:
    st.subheader("Appointments")

    adf = read_csv_df(APPOINTMENTS_CSV, APPT_HEADER)
    if adf.empty:
        st.info("No appointments.")
        return

    adf["_start_dt"] = adf["start_iso"].apply(parse_iso_any)
    adf = adf.sort_values(by="_start_dt", ascending=False).drop(columns=["_start_dt"], errors="ignore")

    st.dataframe(adf[["appointment_id", "customer_name", "customer_phone", "start_iso", "status", "service"]], width="stretch")

    st.markdown("### Send SMS now")
    appt_id = st.selectbox("Appointment", adf["appointment_id"].tolist(), key="send_appt_select")
    msg_type = st.selectbox("Message type", ["new", "moved", "cancelled", "reminder_day", "reminder_2h"], key="send_msg_type")

    if st.button("Queue SMS now", key="queue_now_btn"):
        r = adf[adf["appointment_id"] == appt_id].iloc[0].to_dict()
        start_utc = parse_iso_any(r["start_iso"]) or now_utc()
        start_local = to_local(start_utc, tz)
        tpl = templates.get(msg_type) or templates["new"]
        body = render_tpl(tpl, r["customer_name"], start_local)
        ok = outbox_enqueue(appt_id, r["customer_phone"], body, msg_type, now_utc())
        st.success("Queued." if ok else "Already queued (deduped).")

    st.markdown("### Cancel appointment")
    appt_cancel = st.selectbox("Cancel appointment", adf["appointment_id"].tolist(), key="cancel_appt_select")
    if st.button("Cancel selected", key="cancel_btn"):
        idx = adf.index[adf["appointment_id"] == appt_cancel].tolist()
        if idx:
            i = idx[0]
            adf.at[i, "status"] = "cancelled"
            adf.at[i, "updated_at_iso"] = iso_utc(now_utc())
            write_csv_df(APPOINTMENTS_CSV, adf, APPT_HEADER)
            st.success("Cancelled.")
            st.rerun()


def page_outbox() -> None:
    st.subheader("Outbox")
    df = outbox_state_df()
    if df.empty:
        st.info("Outbox is empty.")
        return

    c1, c2 = st.columns([1, 2])
    with c1:
        if st.button("Process due now", key="process_due_btn"):
            sent, failed, due = process_due_outbox()
            st.success(f"Processed due={due}: sent={sent}, failed={failed} (provider={_effective_provider()})")

    st.dataframe(df.sort_values(by="scheduled_for_iso", ascending=False), width="stretch")


def page_customers() -> None:
    st.subheader("Customers")
    cdf = read_csv_df(CUSTOMERS_CSV, CUSTOMER_HEADER)
    st.dataframe(cdf, width="stretch")

    with st.expander("➕ Add customer", expanded=False):
        name = st.text_input("Name", key="cust_name")
        phone = st.text_input("Phone", key="cust_phone")
        notes = st.text_area("Notes", key="cust_notes")
        consent = st.selectbox("Consent", ["1", "0"], index=0, key="cust_consent")

        if st.button("Create customer", key="cust_create_btn"):
            ts = iso_utc(now_utc())
            row = {
                "customer_id": str(uuid.uuid4()),
                "name": name.strip(),
                "phone": to_e164_heuristic(phone),
                "notes": notes.strip(),
                "consent": consent,
                "created_at_iso": ts,
                "updated_at_iso": ts,
            }
            cdf = pd.concat([cdf, pd.DataFrame([row])], ignore_index=True)
            write_csv_df(CUSTOMERS_CSV, cdf, CUSTOMER_HEADER)
            st.success("Customer created.")
            st.rerun()


def page_templates(templates: Dict[str, str]) -> None:
    st.subheader("Templates")
    tpls = dict(templates)

    tpls["new"] = st.text_area("New template", value=tpls["new"], height=80, key="tpl_new")
    tpls["reminder_day"] = st.text_area("Reminder 24h template", value=tpls["reminder_day"], height=70, key="tpl_day")
    tpls["reminder_2h"] = st.text_area("Reminder 2h template", value=tpls["reminder_2h"], height=70, key="tpl_2h")
    tpls["moved"] = st.text_area("Moved template", value=tpls["moved"], height=70, key="tpl_moved")
    tpls["cancelled"] = st.text_area("Cancelled template", value=tpls["cancelled"], height=70, key="tpl_cancelled")

    if st.button("Save templates", key="tpl_save_btn"):
        save_templates(tpls)
        st.success("Saved.")
        st.rerun()


# =============================================================================
# MAIN
# =============================================================================
def main() -> None:
    st.set_page_config(page_title="Calendo SMS", layout="wide")

    tenant_slug = (os.getenv("TENANT_SLUG") or TENANT_SLUG or "default").strip().lower()
    tz = get_app_tz()

    # Portal settings + creds
    portal_applied = _apply_portal_settings_to_env()

    # License check
    internal_key = (os.getenv("INTERNAL_API_KEY") or "").strip()
    lic_headers = {"X-Internal-Key": internal_key} if internal_key else {}
    lic = _fetch_portal_json(f"/api/license?tenant={tenant_slug}", headers=lic_headers) or {}

    if lic and not bool(lic.get("active", True)):
        st.error("This product license/trial has expired. Open Therapy Portal → Clinic Setup → License to renew.")
        st.stop()

    bootstrap()
    templates = load_templates()

    st.title(f"SMS Calendar (tenant: {tenant_slug})")

    with st.sidebar:
        st.header("Diagnostics")
        st.json(
            {
                "tenant": tenant_slug,
                "portal_base": _portal_base_url(),
                "provider": _effective_provider(),
                "internal_key_set": bool(internal_key),
            }
        )

    tabs = st.tabs(["📅 Calendar", "📋 Appointments", "📨 Outbox", "🧑 Customers", "✍️ Templates"])
    with tabs[0]:
        page_calendar(templates, tz)
    with tabs[1]:
        page_appointments(templates, tz)
    with tabs[2]:
        page_outbox()
    with tabs[3]:
        page_customers()
    with tabs[4]:
        page_templates(templates)


if __name__ == "__main__":
    main()
