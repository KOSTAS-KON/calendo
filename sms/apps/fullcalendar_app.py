# -*- coding: utf-8 -*-
"""
sms3 – Clinic calendar + messaging (Streamlit)

This revision fixes MOVE/CANCEL sync + SMS end-to-end:

✅ New appointments: from booking panel or selecting a slot on the calendar
   - Saves to appointments.csv
   - Enqueues SMS (new) + optional reminders (1 day before, 2 hours before)

✅ Move appointments:
   - Drag/drop or resize in calendar updates appointments.csv
   - Booking/Edit panel auto-syncs to the moved appointment (no button needed)
   - Enqueues "moved" SMS (if enabled) and re-schedules reminders for the new time
   - Soft-deletes queued FUTURE reminders for the old time

✅ Cancel appointments:
   - From Booking/Edit panel: marks appointment as cancelled
   - Calendar updates (event prefixed with ❌ and becomes non-editable)
   - Enqueues "cancelled" SMS (if enabled)
   - Soft-deletes queued FUTURE messages that no longer make sense (reminders, moved/new)

✅ Avoid Streamlit session_state mutation-after-widget errors:
   - Form is prefilled ONLY before any form widgets are created in the current run.

ENV (Infobip):
  SMS_PROVIDER=infobip
  INFOBIP_BASE_URL=4kvd9p.api.infobip.com   (scheme optional; app normalizes to https://)
  INFOBIP_API_KEY=...
  INFOBIP_FROM=YourApprovedSenderIdOrNumber
"""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
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

# ----------------------------
# Access control (SSO)
# ----------------------------
# This app is designed to be opened from the Therapy Portal.
# The Portal issues a short-lived signed token (?sso=...) that prevents
# unauthorized access via a copied direct URL.
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired  # type: ignore

def _get_query_params() -> dict:
    try:
        # Streamlit >= 1.33
        return dict(st.query_params)
    except Exception:
        try:
            return {k: v[0] if isinstance(v, list) and v else v for k, v in (st.experimental_get_query_params() or {}).items()}
        except Exception:
            return {}

def _require_sso() -> tuple[str, bool]:
    qp = _get_query_params()
    tenant = (qp.get("tenant") or "default")
    if isinstance(tenant, list):
        tenant = tenant[0] if tenant else "default"
    tenant = str(tenant).strip().lower() or "default"

    # Make tenant available to provider secrets fetch (Portal internal endpoint expects ?tenant=...)
    os.environ["TENANT_SLUG"] = tenant

    sso = qp.get("sso") or ""
    if isinstance(sso, list):
        sso = sso[0] if sso else ""
    sso = str(sso).strip()

    # Cached session: allow refreshes without re-validating on every rerun
    if st.session_state.get("_sso_ok") and st.session_state.get("_tenant") == tenant:
        return tenant, True

    secret = (os.getenv("SSO_SHARED_SECRET") or os.getenv("SESSION_SECRET") or os.getenv("SECRET_KEY") or "").strip()
    if not secret:
        st.error("Security is not configured: missing SSO_SHARED_SECRET. Please set it in the environment.")
        st.stop()

    max_age = int(os.getenv("SSO_MAX_AGE_SECONDS") or "900")  # 15 minutes
    ser = URLSafeTimedSerializer(secret_key=secret, salt="calendo-sms-sso-v1")

    if not sso:
        st.warning("Please open the SMS Calendar from the Therapy Portal (you are missing an access token).")
        st.markdown("- Go to the Portal → **Suite** → open **SMS Calendar**")
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



# ----------------------------
# Paths / constants
# ----------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
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
    "end_iso",  # UTC ISO
    "status",  # active|cancelled
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
    "consent",  # 0/1
    "created_at_iso",
    "updated_at_iso",
]

DEFAULT_TEMPLATES = {
    "new": (
        "Νέο ραντεβού: {name} στις {date} {time}.\n"
        "Διεύθυνση: {address}\n"
        "Χάρτης: {map}"
    ),
    "reminder_day": "Υπενθύμιση: {name} έχεις ραντεβού αύριο {date} στις {time}.",
    "reminder_2h": "Υπενθύμιση: {name} σε 2 ώρες έχεις ραντεβού στις {time}.",
    "moved": "Ενημέρωση: {name} το ραντεβού μεταφέρθηκε σε {date} {time}.",
    "cancelled": "Ακύρωση: {name} το ραντεβού της {date} {time} ακυρώθηκε.",
}

FORM_KEYS = {
    # stable id + a human label (label is only for display)
    "customer_id": "form_customer_id",
    "customer_label": "form_customer_label",
    "service": "form_service",
    "date": "form_date",
    "time": "form_time",
    "duration": "form_duration",
    "notes": "form_notes",
    "pref_new_now": "form_pref_new_now",
    "pref_day": "form_pref_day",
    "pref_2h": "form_pref_2h",
}

# calendar selection -> create new appointment
LAST_SELECT_SIG = "__last_calendar_select_sig"
LAST_SELECT_START_UTC = "__last_calendar_select_start_utc"
LAST_SELECT_END_UTC = "__last_calendar_select_end_utc"
LAST_SELECT_LABEL = "__last_calendar_select_local_label"

LAST_CAL_ACTION_SIG = "__last_cal_action_sig"

# current selection (edit mode)
SELECTED_APPT_ID = "__selected_appointment_id"
LOADED_APPT_ID = "__loaded_appointment_id"  # when prefill was last applied

PROVIDER_OVERRIDE_KEY = "__provider_override"  # session_state


# ----------------------------
# .env loader
# ----------------------------
def load_dotenv_simple(
    dotenv_path: Path, *, override: bool = False, override_prefixes: Optional[List[str]] = None
) -> None:
    if not dotenv_path.exists():
        return
    override_prefixes = override_prefixes or []
    try:
        for raw in dotenv_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if not k:
                continue
            must_override = override or any(k.startswith(pfx) for pfx in override_prefixes)
            if must_override or (k not in os.environ):
                os.environ[k] = v
    except Exception:
        return


# ----------------------------
# Timezone helpers
# ----------------------------
def get_app_tz() -> Any:
    tz_name = os.getenv("APP_TIMEZONE", "Europe/Athens")
    if ZoneInfo:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            return timezone.utc
    return timezone.utc


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


def to_local(dt: datetime, app_tz: Any) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(app_tz)


def to_utc(dt: datetime, app_tz: Any) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=app_tz)
    return dt.astimezone(timezone.utc)


# ----------------------------
# Phone helpers (E.164)
# ----------------------------
def normalize_phone(phone: str) -> str:
    if not phone:
        return ""
    p = str(phone).strip()
    p = re.sub(r"[^\d+]", "", p)
    return p


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


def valid_phone_e164(phone: str) -> bool:
    p = to_e164_heuristic(phone)
    if not p.startswith("+"):
        return False
    digits = re.sub(r"\D", "", p)
    return 8 <= len(digits) <= 15


# ----------------------------
# CSV helpers
# ----------------------------
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


def normalize_customers(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    ts = iso_utc(now_utc())
    for i in df.index:
        if not str(df.at[i, "customer_id"]).strip():
            df.at[i, "customer_id"] = str(uuid.uuid4())
        if str(df.at[i, "consent"]).strip() == "":
            df.at[i, "consent"] = "1"
        if str(df.at[i, "created_at_iso"]).strip() == "":
            df.at[i, "created_at_iso"] = ts
        if str(df.at[i, "updated_at_iso"]).strip() == "":
            df.at[i, "updated_at_iso"] = ts
        df.at[i, "phone"] = normalize_phone(df.at[i, "phone"])
    return df


def normalize_appointments(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    defaults = {
        "status": "active",
        "pref_send_new_now": "1",
        "pref_reminder_day": "0",
        "pref_reminder_2h": "0",
        "pref_send_moved_now": "1",
        "pref_send_cancel_now": "1",
    }
    ts = iso_utc(now_utc())
    for i in df.index:
        if not str(df.at[i, "appointment_id"]).strip():
            df.at[i, "appointment_id"] = str(uuid.uuid4())
        if str(df.at[i, "status"]).strip() == "":
            df.at[i, "status"] = defaults["status"]

        ph = str(df.at[i, "customer_phone"]).strip()
        if ph:
            df.at[i, "customer_phone"] = to_e164_heuristic(ph)

        for k, dv in defaults.items():
            if str(df.at[i, k]).strip() == "":
                df.at[i, k] = dv

        if str(df.at[i, "created_at_iso"]).strip() == "":
            df.at[i, "created_at_iso"] = ts
        if str(df.at[i, "updated_at_iso"]).strip() == "":
            df.at[i, "updated_at_iso"] = ts
    return df


def safe_int01(x: Any) -> int:
    try:
        return 1 if int(x) == 1 else 0
    except Exception:
        return 0


def consent01(x: Any) -> int:
    if x is None:
        return 1
    s = str(x).strip().lower()
    if s in ("", "nan", "none"):
        return 1
    try:
        return 1 if int(s) == 1 else 0
    except Exception:
        return 1


# ----------------------------
# Outbox JSONL (event-sourced)
# ----------------------------
def outbox_jsonl_ensure() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
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
                "ts_iso": str(r.get("ts_iso", "")),
                "scheduled_for_iso": str(r.get("scheduled_for_iso", "")),
                "appointment_id": str(r.get("appointment_id", "")),
                "customer_id": str(r.get("customer_id", "")),
                "to": str(r.get("to", "")),
                "channel": str(r.get("channel", "sms")),
                "message_type": str(r.get("message_type", "")),
                "body": str(r.get("body", "")),
                "status": str(r.get("status", "")),
                "provider": str(r.get("provider", "")),
                "provider_msgid": str(r.get("provider_msgid", "")),
                "provider_status": str(r.get("provider_status", "")),
                "error": str(r.get("error", "")),
                "dedupe_key": str(r.get("dedupe_key", "")),
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(
            columns=[
                "outbox_id",
                "ts_iso",
                "scheduled_for_iso",
                "appointment_id",
                "customer_id",
                "to",
                "channel",
                "message_type",
                "body",
                "status",
                "provider",
                "provider_msgid",
                "provider_status",
                "error",
                "dedupe_key",
            ]
        )
    
    # Safety: ensure outbox_id is always a column (some pandas ops might turn it into index)
    if "outbox_id" not in df.columns:
        if getattr(df.index, "name", None) == "outbox_id":
            df = df.reset_index()
        else:
            df["outbox_id"] = ""

    return df


def outbox_dedupe_key(appointment_id: str, message_type: str, scheduled_for_iso: str) -> str:
    return f"{appointment_id}|{message_type}|{scheduled_for_iso}".strip("|")


def outbox_enqueue(
    appointment_id: str,
    customer_id: str,
    to: str,
    body: str,
    *,
    channel: str = "sms",
    message_type: str = "new",
    scheduled_for_utc: Optional[datetime] = None,
) -> bool:
    scheduled = scheduled_for_utc or now_utc()
    scheduled_iso = iso_utc(scheduled)

    to_e164 = to_e164_heuristic(to)
    dk = outbox_dedupe_key(appointment_id, message_type, scheduled_iso)

    df = outbox_state_df()
    if not df.empty and (df["dedupe_key"] == dk).any():
        return False

    oid = str(uuid.uuid4())
    ev = {
        "outbox_id": oid,
        "ts_iso": iso_utc(now_utc()),
        "scheduled_for_iso": scheduled_iso,
        "appointment_id": appointment_id,
        "customer_id": customer_id,
        "to": to_e164,
        "channel": channel,
        "message_type": message_type,
        "body": body,
        "status": "queued",
        "provider": "",
        "provider_msgid": "",
        "provider_status": "",
        "error": "",
        "dedupe_key": dk,
    }
    outbox_jsonl_append(ev)
    return True


def outbox_mark(
    outbox_id: str,
    *,
    status: str,
    provider: str,
    provider_msgid: str = "",
    provider_status: str = "",
    error: str = "",
) -> None:
    ev = {
        "outbox_id": outbox_id,
        "ts_iso": iso_utc(now_utc()),
        "status": status,
        "provider": provider,
        "provider_msgid": provider_msgid,
        "provider_status": provider_status,
        "error": error,
    }
    outbox_jsonl_append(ev)


def outbox_delete(outbox_id: str) -> None:
    outbox_mark(
        outbox_id,
        status="deleted",
        provider=effective_provider(),
        provider_msgid="",
        provider_status="",
        error="user_deleted",
    )


def outbox_delete_queued_for_appt(appointment_id: str, message_types: Optional[List[str]] = None) -> int:
    df = outbox_state_df()
    if df.empty:
        return 0
    now = now_utc()

    q = df[df["status"] == "queued"].copy()
    q = q[q["appointment_id"].astype(str) == str(appointment_id)]
    if message_types:
        q = q[q["message_type"].isin(message_types)]

    def is_future(s: str) -> bool:
        dt = parse_iso_any(s)
        return bool(dt and dt.astimezone(timezone.utc) >= now)

    q = q[q["scheduled_for_iso"].apply(is_future)]
    count = 0
    if "outbox_id" not in q.columns:
        # nothing to delete (schema mismatch / empty)
        return 0
    for oid in q["outbox_id"].tolist():
        outbox_delete(str(oid))
        count += 1
    return count


def pref_enabled(appt: Dict[str, str], key: str, default: int = 1) -> int:
    v = appt.get(key, "")
    if v is None or str(v).strip() == "":
        return default
    return safe_int01(v)


# ----------------------------
# Templates
# ----------------------------
def load_templates() -> Dict[str, str]:
    if not TEMPLATES_JSON.exists():
        TEMPLATES_JSON.write_text(json.dumps(DEFAULT_TEMPLATES, ensure_ascii=False, indent=2), encoding="utf-8")
        return dict(DEFAULT_TEMPLATES)
    try:
        data = json.loads(TEMPLATES_JSON.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("templates.json not dict")
        for k, v in DEFAULT_TEMPLATES.items():
            data.setdefault(k, v)
        return {str(k): str(v) for k, v in data.items()}
    except Exception:
        return dict(DEFAULT_TEMPLATES)


def save_templates(templates: Dict[str, str]) -> None:
    TEMPLATES_JSON.write_text(json.dumps(templates, ensure_ascii=False, indent=2), encoding="utf-8")


def render_template_flexible(tpl: str, *, customer_name: str, appt_start_local: datetime) -> str:
    address = os.getenv("CLINIC_ADDRESS", "Antoniou Kamara 3, Veria 591 32, Greece")
    map_link = os.getenv("CLINIC_MAP_LINK", "https://maps.google.com/?q=Antoniou+Kamara+3,+Veria+591+32,+Greece")

    date_s = appt_start_local.strftime("%Y-%m-%d")
    time_s = appt_start_local.strftime("%H:%M")
    out = tpl
    out = out.replace("{name}", customer_name or "")
    out = out.replace("{date}", date_s)
    out = out.replace("{time}", time_s)
    out = out.replace("{address}", address)
    out = out.replace("{map}", map_link)
    return out


# ----------------------------
# Provider logging + Infobip
# ----------------------------
def provider_log(payload: Dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with PROVIDER_DEBUG_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def redact(s: str, keep: int = 4) -> str:
    if not s:
        return ""
    if len(s) <= keep * 2:
        return "***"
    return s[:keep] + "***" + s[-keep:]


def normalize_infobip_base_url(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if not s.startswith("http://") and not s.startswith("https://"):
        s = "https://" + s
    return s.rstrip("/")


def effective_provider() -> str:
    """Resolve which SMS provider to use.

    Priority:
      1) UI override (session_state) if set to mock/infobip
      2) SMS_PROVIDER environment variable if set
      3) If Infobip credentials are present (after portal apply), use infobip
      4) Fallback to mock
    """
    ov_raw = str(st.session_state.get(PROVIDER_OVERRIDE_KEY, "")).strip()
    ov = ov_raw.lower()
    if ov in ("mock", "infobip"):
        return ov

    env_provider = (os.getenv("SMS_PROVIDER") or "").strip().lower()
    if env_provider in ("mock", "infobip"):
        return env_provider

    # If Infobip creds are present, prefer Infobip automatically.
    base = normalize_infobip_base_url(os.getenv("INFOBIP_BASE_URL") or "")
    key = (os.getenv("INFOBIP_API_KEY") or "").strip()
    from_ = (os.getenv("INFOBIP_FROM") or "").strip()
    if base and key and from_:
        return "infobip"

    return "mock"


def send_sms_mock(to_e164: str, body: str) -> str:
    if not valid_phone_e164(to_e164):
        raise ValueError(f"Invalid phone (need E.164): '{to_e164}'")
    msgid = "mock_" + uuid.uuid4().hex[:12]
    provider_log(
        {
            "ts": iso_utc(now_utc()),
            "provider": "mock",
            "channel": "sms",
            "to": to_e164,
            "body": body,
            "status": "sent_mock",
            "provider_msgid": msgid,
        }
    )
    return msgid


def send_sms_infobip(to_e164: str, body: str) -> Tuple[str, str]:
    if not valid_phone_e164(to_e164):
        raise ValueError(f"Invalid phone (need E.164): '{to_e164}'")

    base = normalize_infobip_base_url(os.getenv("INFOBIP_BASE_URL") or "")
    key = (os.getenv("INFOBIP_API_KEY") or "").strip()
    from_ = (os.getenv("INFOBIP_FROM") or "").strip()

    if not (base and key and from_):
        raise ValueError("Infobip not configured. Set INFOBIP_BASE_URL, INFOBIP_API_KEY, INFOBIP_FROM")

    url = f"{base}/sms/2/text/advanced"
    headers = {
        "Authorization": f"App {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {"messages": [{"from": from_, "destinations": [{"to": to_e164}], "text": body}]}

    provider_log(
        {
            "ts": iso_utc(now_utc()),
            "provider": "infobip",
            "stage": "request",
            "url": url,
            "headers": {
                "Authorization": f"App {redact(key)}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            "payload": payload,
        }
    )

    r = requests.post(url, headers=headers, json=payload, timeout=30)

    provider_log(
        {
            "ts": iso_utc(now_utc()),
            "provider": "infobip",
            "stage": "response",
            "http_status": r.status_code,
            "text_snippet": (r.text or "")[:1200],
        }
    )

    if r.status_code not in (200, 201):
        raise RuntimeError(f"Infobip HTTP {r.status_code}: {(r.text or '')[:600]}")

    msgid = ""
    status_name = ""
    try:
        j = r.json()
        msg = j["messages"][0]
        msgid = str(msg.get("messageId") or "")
        stobj = msg.get("status") or {}
        status_name = str(stobj.get("name") or "")
    except Exception:
        pass

    if not msgid:
        msgid = "infobip_" + uuid.uuid4().hex[:12]

    provider_log(
        {
            "ts": iso_utc(now_utc()),
            "provider": "infobip",
            "channel": "sms",
            "to": to_e164,
            "status": "accepted",
            "provider_msgid": msgid,
            "infobip_status_name": status_name,
        }
    )
    return msgid, status_name


def send_via_provider(to_e164: str, body: str, channel: str = "sms") -> Tuple[str, str, str, str]:
    if channel != "sms":
        raise ValueError("Only the SMS channel is implemented in this version")

    prov = effective_provider()
    if prov == "mock":
        msgid = send_sms_mock(to_e164, body)
        return msgid, "sent_mock", "mock", "MOCK"
    if prov == "infobip":
        msgid, provider_status = send_sms_infobip(to_e164, body)
        return msgid, "sent", "infobip", provider_status
    raise ValueError(f"Unknown provider '{prov}'")


def process_due_outbox(limit: int = 200) -> Tuple[int, int, int]:
    print('SMS_OUTBOX: process_due_outbox called', flush=True)
    df = outbox_state_df()
    if df.empty:
        return (0, 0, 0)

    now = now_utc()
    sent, failed = 0, 0
    due_rows: List[Dict[str, Any]] = []

    for _, r in df.iterrows():
        if str(r.get("status")) != "queued":
            continue
        sched = parse_iso_any(r.get("scheduled_for_iso"))
        if not sched:
            outbox_mark(str(r.get("outbox_id")), status="failed", provider=effective_provider(), error="Invalid scheduled_for_iso")
            failed += 1
            continue
        if sched.astimezone(timezone.utc) <= now + timedelta(seconds=2):
            due_rows.append(r.to_dict())
        if len(due_rows) >= limit:
            break

    for r in due_rows:
        oid = str(r.get("outbox_id"))
        try:
            to_e164 = str(r.get("to") or "").strip()
            if not valid_phone_e164(to_e164):
                raise ValueError(f"Invalid E.164 in outbox.to: '{to_e164}'")

            msgid, status, prov, pstatus = send_via_provider(to_e164, str(r.get("body") or ""), channel=str(r.get("channel") or "sms"))
            outbox_mark(oid, status=status, provider=prov, provider_msgid=msgid, provider_status=pstatus, error="")
            sent += 1
        except Exception as e:
            print(f"SMS_OUTBOX: send failed oid={oid} err={str(e)}", flush=True)
            outbox_mark(oid, status="failed", provider=effective_provider(), error=str(e))
            provider_log(
                {
                    "ts": iso_utc(now_utc()),
                    "provider": effective_provider(),
                    "stage": "failed",
                    "to": r.get("to", ""),
                    "body": (str(r.get("body") or "")[:500]),
                    "error": str(e),
                }
            )
            failed += 1

    return (sent, failed, len(due_rows))


def send_due_now_feedback(prefix: str = "") -> Tuple[int, int, int]:
    """Process due messages and show a toast when anything was due.

    `prefix` is used by move/cancel flows so the user can see which action triggered the send.
    """
    sent, failed, due_seen = process_due_outbox()
    print(f"SMS_OUTBOX: due_seen={due_seen} sent={sent} failed={failed} provider={effective_provider()}", flush=True)
    if due_seen > 0:
        p = (prefix or "").strip()
        if p:
            st.toast(
                f"{p} • processed due={due_seen}: sent={sent}, failed={failed} (provider={effective_provider()})",
                icon="📨",
            )
        else:
            st.toast(
                f"Auto-send processed due={due_seen}: sent={sent}, failed={failed} (provider={effective_provider()})",
                icon="📨",
            )
    return (sent, failed, due_seen)


# ----------------------------
# UI style
# ----------------------------

def inject_style() -> None:
    """Align Streamlit look & feel with the Therapy Portal UI theme (CSS only)."""
    st.markdown(
        r"""
<style>
:root{
  --bg: #f6f7fb;
  --panel: #ffffff;
  --text: #0f172a;
  --muted: rgba(15,23,42,.62);
  --border: rgba(15,23,42,.12);
  --border-strong: rgba(15,23,42,.18);
  --shadow: 0 14px 34px rgba(15,23,42,.10);
  --shadow-soft: 0 10px 24px rgba(15,23,42,.08);
  --radius: 18px;
  --radius-lg: 22px;
  --pill: 999px;
  --accent: #0f172a;
  --accent-2: #2563eb;

  /* Topbar sizing (used to keep content from sliding under header) */
  --sms-topbar-h: 64px;
}

/* Streamlit header can overlap custom topbars depending on hosting/frame */
header[data-testid="stHeader"]{
  background: transparent !important;
}

/* Ensure page content starts below our sticky topbar */
.main .block-container{
  padding-top: calc(var(--sms-topbar-h) + 18px) !important;
}

/* App background + layout */
.stApp { background: var(--bg) !important; color: var(--text) !important; }
.block-container { padding-top: 1.0rem; padding-bottom: 2.2rem; max-width: 1280px; }

/* Sidebar */
section[data-testid="stSidebar"]{
  background: var(--panel) !important;
  border-right: 1px solid var(--border) !important;
}
section[data-testid="stSidebar"] .stMarkdown,
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] span{ color: var(--text) !important; }

/* Inputs */
.stTextInput input, .stTextArea textarea, .stNumberInput input,
.stSelectbox div[data-baseweb="select"] > div,
.stDateInput input{
  border-radius: 14px !important;
  border: 1px solid var(--border) !important;
  background: rgba(255,255,255,.98) !important;
}

/* Buttons */
.stButton>button{
  border-radius: 14px !important;
  border: 1px solid var(--border-strong) !important;
  background: var(--panel) !important;
  box-shadow: 0 8px 18px rgba(15,23,42,.06) !important;
  font-weight: 800 !important;
}
.stButton>button:hover{ border-color: rgba(15,23,42,.28) !important; }

/* Tabs (make them look like the Portal pills) */
div[data-testid="stTabs"] [data-baseweb="tab-list"]{
  gap: 10px !important;
  padding: 10px 6px 12px 6px !important;
  border-bottom: 1px solid var(--border) !important;
  overflow-x:auto !important;
}
div[data-testid="stTabs"] [data-baseweb="tab"]{
  min-height: 46px !important;
  border-radius: var(--pill) !important;
  padding: 8px 16px !important;
  border: 1px solid var(--border) !important;
  background: rgba(255,255,255,.92) !important;
  white-space: nowrap !important;
  font-size: 15px !important;
  font-weight: 900 !important;
  color: var(--text) !important;
}
div[data-testid="stTabs"] [aria-selected="true"]{
  border-color: rgba(15,23,42,.28) !important;
  background: rgba(255,255,255,1) !important;
  box-shadow: var(--shadow-soft) !important;
}
div[data-testid="stTabs"] [data-baseweb="tab-highlight"]{ display:none !important; }

/* Card (same style language as Portal cards) */
.card {
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 14px 14px;
  margin-bottom: 12px;
  background: var(--panel);
  box-shadow: var(--shadow-soft);
}
.card h3 { margin: 0 0 10px 0; }

/* Sub-card (for sections inside a card) */
.subcard{
  border: 1px dashed rgba(15,23,42,.18);
  border-radius: 16px;
  padding: 12px 12px;
  background: rgba(255,255,255,.86);
  margin: 10px 0 10px 0;
}

/* Pills / badges */
.pill{
  display:inline-flex; align-items:center; gap:8px;
  padding: 6px 10px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: rgba(255,255,255,.95);
  font-weight: 900;
  font-size: 12px;
  letter-spacing: .02em;
}
.pill.ok{ background: rgba(34,197,94,.10); border-color: rgba(34,197,94,.35); }
.pill.warn{ background: rgba(245,158,11,.10); border-color: rgba(245,158,11,.35); }
.pill.bad{ background: rgba(239,68,68,.10); border-color: rgba(239,68,68,.35); }
.pill.info{ background: rgba(37,99,235,.10); border-color: rgba(37,99,235,.35); }
.pill.muted{ background: rgba(15,23,42,.06); border-color: rgba(15,23,42,.14); }

.pillrow{ display:flex; flex-wrap:wrap; gap:8px; margin: 6px 0 8px 0; }

/* Streamlit widgets inside cards */
div[data-testid="stForm"],
div[data-testid="stExpander"],
div[data-testid="stContainer"]{
  border-radius: 16px;
}

/* Make expanders look like portal sections */
details[data-testid="stExpander"]{
  border: 1px solid var(--border) !important;
  border-radius: 16px !important;
  background: rgba(255,255,255,.92) !important;
  box-shadow: 0 8px 18px rgba(15,23,42,.05) !important;
}

/* Tables / dataframe polish */
.stDataFrame, .stTable {
  border-radius: 16px;
  overflow: hidden;
  border: 1px solid var(--border);
}

/* Headline rhythm */
h1, h2, h3 { letter-spacing: -0.02em; }

/* “Header bar” helper */
.sms-topbar{
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:12px;
  padding: 12px 14px;
  margin: 10px 0 14px 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: rgba(255,255,255,.96);
  backdrop-filter: blur(8px);
  box-shadow: 0 8px 24px rgba(0,0,0,0.06);

  position: sticky;
  top: 0;
  z-index: 9999;
}
.sms-topbar .title{
  display:flex; align-items:center; gap:10px;
  font-weight: 900; font-size: 18px;
}
.sms-topbar .subtitle{ font-size: 13px; color: var(--muted); margin-top: 2px; }
.sms-topbar .links{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.sms-pilllink{
  display:inline-flex; align-items:center; gap:8px;
  padding: 9px 12px; border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--panel);
  text-decoration:none !important;
  color: var(--text) !important;
  font-weight: 850;
}
.sms-pilllink:hover{ border-color: rgba(15,23,42,.28) !important; box-shadow: var(--shadow-soft); }

/* Calendar container polish */
iframe{ border-radius: 16px !important; }
</style>
        """,
        unsafe_allow_html=True,
    )


def card(title: str, body_fn, icon: str = "🧩") -> None:
    st.markdown(f'<div class="card"><h3>{icon} {title}</h3>', unsafe_allow_html=True)
    body_fn()
    st.markdown("</div>", unsafe_allow_html=True)


def pill(text: str, tone: str = "muted") -> str:
    tone = (tone or "muted").strip().lower()
    if tone not in {"ok", "warn", "bad", "info", "muted"}:
        tone = "muted"
    return f'<span class="pill {tone}">{text}</span>'


def pill_row(pills: List[str]) -> None:
    html = "".join(pills)
    st.markdown(f'<div class="pillrow">{html}</div>', unsafe_allow_html=True)


def subcard_open(title: str, icon: str = "") -> None:
    head = f"{icon} {title}".strip()
    st.markdown(f'<div class="subcard"><div style="font-weight:900; margin:0 0 8px 0;">{head}</div>', unsafe_allow_html=True)


def subcard_close() -> None:
    st.markdown("</div>", unsafe_allow_html=True)


# ----------------------------
# Calendar helpers
# ----------------------------
def build_calendar_events_local(appts: pd.DataFrame, app_tz: Any) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for _, r in appts.iterrows():
        s_utc = parse_iso_any(r["start_iso"])
        e_utc = parse_iso_any(r["end_iso"])
        if not s_utc or not e_utc:
            continue
        s_loc = to_local(s_utc, app_tz)
        e_loc = to_local(e_utc, app_tz)
        if r["status"] == "cancelled":
            continue
        title = f'{r["customer_name"]} • {r["service"]}'
        events.append(
            {
                "id": r["appointment_id"],
                "title": title,
                "start": s_loc.isoformat(),
                "end": e_loc.isoformat(),
                "editable": (r["status"] == "active"),
            }
        )
    return events


def parse_calendar_action(cal_state: Any) -> Dict[str, Any]:
    if not isinstance(cal_state, dict) or not cal_state:
        return {"type": None}
    cb = cal_state.get("callback")

    if cb == "select":
        sel = cal_state.get("select") or {}
        return {"type": "select", "start": sel.get("start"), "end": sel.get("end")}

    if cb == "eventClick":
        ev = cal_state.get("event") or {}
        return {"type": "eventClick", "id": ev.get("id")}

    if cb in ("eventDrop", "eventResize"):
        ev = cal_state.get("event") or {}
        return {"type": cb, "id": ev.get("id"), "start": ev.get("start"), "end": ev.get("end")}

    return {"type": None}


def init_form_defaults(customers: pd.DataFrame, app_tz: Any) -> None:
    st.session_state.setdefault(SELECTED_APPT_ID, "")
    st.session_state.setdefault(LOADED_APPT_ID, "")

    if FORM_KEYS["customer_id"] not in st.session_state:
        if not customers.empty:
            first = customers.iloc[0]
            st.session_state[FORM_KEYS["customer_id"]] = str(first["customer_id"])
        else:
            st.session_state[FORM_KEYS["customer_id"]] = ""

    st.session_state.setdefault(FORM_KEYS["service"], "Session")
    st.session_state.setdefault(FORM_KEYS["date"], to_local(now_utc(), app_tz).date())
    st.session_state.setdefault(FORM_KEYS["time"], (to_local(now_utc(), app_tz) + timedelta(hours=1)).time().replace(second=0, microsecond=0))
    st.session_state.setdefault(FORM_KEYS["duration"], 30)
    st.session_state.setdefault(FORM_KEYS["notes"], "")

    st.session_state.setdefault(FORM_KEYS["pref_new_now"], True)
    st.session_state.setdefault(FORM_KEYS["pref_day"], False)
    st.session_state.setdefault(FORM_KEYS["pref_2h"], False)
def apply_calendar_selection_into_booking(start_utc: datetime, end_utc: datetime, app_tz: Any) -> None:
    sig = f"{start_utc.isoformat()}|{end_utc.isoformat()}"
    if st.session_state.get(LAST_SELECT_SIG) == sig:
        return

    start_loc = to_local(start_utc, app_tz)
    end_loc = to_local(end_utc, app_tz)

    dur = max(15, int((end_loc - start_loc).total_seconds() // 60))
    common = [15, 30, 45, 60, 90, 120]
    duration_pick = min(common, key=lambda x: abs(x - dur))

    st.session_state[FORM_KEYS["date"]] = start_loc.date()
    st.session_state[FORM_KEYS["time"]] = start_loc.time().replace(second=0, microsecond=0)
    st.session_state[FORM_KEYS["duration"]] = duration_pick

    st.session_state[LAST_SELECT_SIG] = sig
    st.session_state[LAST_SELECT_START_UTC] = start_utc.astimezone(timezone.utc).isoformat()
    st.session_state[LAST_SELECT_END_UTC] = end_utc.astimezone(timezone.utc).isoformat()
    st.session_state[LAST_SELECT_LABEL] = f"{start_loc.strftime('%Y-%m-%d %H:%M')} → {end_loc.strftime('%H:%M')} ({os.getenv('APP_TIMEZONE','Europe/Athens')})"


def collision_exists(appts: pd.DataFrame, start_utc: datetime, end_utc: datetime, ignore_appt_id: Optional[str] = None) -> bool:
    def overlap(a_s: datetime, a_e: datetime, b_s: datetime, b_e: datetime) -> bool:
        return a_s < b_e and b_s < a_e

    for _, r in appts[appts["status"] == "active"].iterrows():
        if ignore_appt_id and str(r["appointment_id"]) == str(ignore_appt_id):
            continue
        rs = parse_iso_any(r["start_iso"])
        re_ = parse_iso_any(r["end_iso"])
        if not rs or not re_:
            continue
        if overlap(start_utc, end_utc, rs.astimezone(timezone.utc), re_.astimezone(timezone.utc)):
            return True
    return False


def clamp_future(dt_utc: datetime) -> Optional[datetime]:
    return None if dt_utc < now_utc() else dt_utc


def get_appointment(appts: pd.DataFrame, appointment_id: str) -> Optional[Dict[str, str]]:
    if appts.empty:
        return None
    m = appts[appts["appointment_id"].astype(str) == str(appointment_id)]
    if m.empty:
        return None
    return m.iloc[0].to_dict()  # type: ignore


def update_appointment(appts: pd.DataFrame, appointment_id: str, updates: Dict[str, str]) -> pd.DataFrame:
    appts = appts.copy()
    ix = appts.index[appts["appointment_id"].astype(str) == str(appointment_id)]
    if len(ix) == 0:
        return appts
    i = ix[0]
    for k, v in updates.items():
        if k in appts.columns:
            appts.at[i, k] = v
    appts.at[i, "updated_at_iso"] = iso_utc(now_utc())
    return appts


def enqueue_for_new(appt: Dict[str, str], templates: Dict[str, str], app_tz: Any) -> int:
    added = 0
    start_utc = parse_iso_any(appt["start_iso"]) or now_utc()
    start_loc = to_local(start_utc, app_tz)

    if safe_int01(appt.get("pref_send_new_now", "1")) == 1:
        body = render_template_flexible(templates["new"], customer_name=appt["customer_name"], appt_start_local=start_loc)
        if outbox_enqueue(appt["appointment_id"], appt["customer_id"], appt["customer_phone"], body, message_type="new", scheduled_for_utc=now_utc() - timedelta(seconds=1)):
            added += 1

    if safe_int01(appt.get("pref_reminder_day", "0")) == 1:
        when = clamp_future(start_utc.astimezone(timezone.utc) - timedelta(days=1))
        if when:
            body = render_template_flexible(templates["reminder_day"], customer_name=appt["customer_name"], appt_start_local=start_loc)
            if outbox_enqueue(appt["appointment_id"], appt["customer_id"], appt["customer_phone"], body, message_type="reminder_day", scheduled_for_utc=when):
                added += 1

    if safe_int01(appt.get("pref_reminder_2h", "0")) == 1:
        when = clamp_future(start_utc.astimezone(timezone.utc) - timedelta(hours=2))
        if when:
            body = render_template_flexible(templates["reminder_2h"], customer_name=appt["customer_name"], appt_start_local=start_loc)
            if outbox_enqueue(appt["appointment_id"], appt["customer_id"], appt["customer_phone"], body, message_type="reminder_2h", scheduled_for_utc=when):
                added += 1

    return added


def reschedule_reminders(appt: Dict[str, str], templates: Dict[str, str], app_tz: Any) -> int:
    added = 0
    start_utc = parse_iso_any(appt["start_iso"]) or now_utc()
    start_loc = to_local(start_utc, app_tz)

    if safe_int01(appt.get("pref_reminder_day", "0")) == 1:
        when = clamp_future(start_utc.astimezone(timezone.utc) - timedelta(days=1))
        if when:
            body = render_template_flexible(templates["reminder_day"], customer_name=appt["customer_name"], appt_start_local=start_loc)
            if outbox_enqueue(appt["appointment_id"], appt["customer_id"], appt["customer_phone"], body, message_type="reminder_day", scheduled_for_utc=when):
                added += 1

    if safe_int01(appt.get("pref_reminder_2h", "0")) == 1:
        when = clamp_future(start_utc.astimezone(timezone.utc) - timedelta(hours=2))
        if when:
            body = render_template_flexible(templates["reminder_2h"], customer_name=appt["customer_name"], appt_start_local=start_loc)
            if outbox_enqueue(appt["appointment_id"], appt["customer_id"], appt["customer_phone"], body, message_type="reminder_2h", scheduled_for_utc=when):
                added += 1

    return added


def enqueue_for_moved(appt: Dict[str, str], templates: Dict[str, str], app_tz: Any, *, force: bool = False) -> int:
    """Queue an immediate 'moved' SMS (one‑click). If force=True ignore stored pref flag."""
    if (not force) and pref_enabled(appt, "pref_send_moved_now", default=1) != 1:
        return 0
    start_utc = parse_iso_any(appt.get("start_iso")) or now_utc()
    start_loc = to_local(start_utc, app_tz)
    body = render_template_flexible(
        templates["moved"],
        customer_name=appt.get("customer_name", ""),
        appt_start_local=start_loc,
    )
    return 1 if outbox_enqueue(
        appt.get("appointment_id", ""),
        appt.get("customer_id", ""),
        appt.get("customer_phone", ""),
        body,
        message_type="moved",
        scheduled_for_utc=now_utc() - timedelta(seconds=1),
    ) else 0
def enqueue_for_cancelled(appt: Dict[str, str], templates: Dict[str, str], app_tz: Any, *, force: bool = False) -> int:
    """Queue an immediate 'cancelled' SMS (one‑click). If force=True ignore stored pref flag."""
    if (not force) and pref_enabled(appt, "pref_send_cancel_now", default=1) != 1:
        return 0
    start_utc = parse_iso_any(appt.get("start_iso")) or now_utc()
    start_loc = to_local(start_utc, app_tz)
    body = render_template_flexible(
        templates["cancelled"],
        customer_name=appt.get("customer_name", ""),
        appt_start_local=start_loc,
    )
    return 1 if outbox_enqueue(
        appt.get("appointment_id", ""),
        appt.get("customer_id", ""),
        appt.get("customer_phone", ""),
        body,
        message_type="cancelled",
        scheduled_for_utc=now_utc() - timedelta(seconds=1),
    ) else 0
def page_calendar(templates: Dict[str, str], app_tz: Any) -> None:
    st.subheader("📅 Ημερολόγιο")

    customers = normalize_customers(read_csv_df(CUSTOMERS_CSV, CUSTOMER_HEADER))
    write_csv_df(CUSTOMERS_CSV, customers, CUSTOMER_HEADER)

    appts = normalize_appointments(read_csv_df(APPOINTMENTS_CSV, APPT_HEADER))
    write_csv_df(APPOINTMENTS_CSV, appts, APPT_HEADER)

    init_form_defaults(customers, app_tz)

    st.divider()

    if st_calendar is None:
        st.warning("Install: pip install streamlit-calendar")
    else:
        events = build_calendar_events_local(appts, app_tz)
        cal_options = {
            "initialView": "timeGridWeek",
            "timeZone": "local",
            "headerToolbar": {"left": "prev,next today", "center": "title", "right": "dayGridMonth,timeGridWeek,timeGridDay"},
            "editable": True,
            "selectable": True,
            "selectMirror": True,
            "height": 720,
        }
        cal_payload = st_calendar(events=events, options=cal_options, key="main_calendar")
        action = parse_calendar_action(cal_payload)

        # Guard against the calendar component repeating the last callback on reruns.
        # Without this, the app may get stuck re-processing the same eventClick/eventDrop and never render other tabs.
        if action.get("type"):
            sig = "|".join([
                str(action.get("type") or ""),
                str(action.get("id") or ""),
                str(action.get("start") or ""),
                str(action.get("end") or ""),
            ])
            if st.session_state.get(LAST_CAL_ACTION_SIG) == sig:
                action = {"type": None}
            else:
                st.session_state[LAST_CAL_ACTION_SIG] = sig

        # select slot -> prefill new appointment time
        if action["type"] == "select":
            s = parse_iso_any(action.get("start"))
            e = parse_iso_any(action.get("end"))
            if s and e:
                s_utc = s.astimezone(timezone.utc) if s.tzinfo else to_utc(s, app_tz)
                e_utc = e.astimezone(timezone.utc) if e.tzinfo else to_utc(e, app_tz)
                apply_calendar_selection_into_booking(s_utc, e_utc, app_tz)
                # Selecting an empty slot implies "new appointment" mode.
                st.session_state[SELECTED_APPT_ID] = ""
                st.session_state[LOADED_APPT_ID] = ""

        # click event -> select appointment and sync panel
        if action["type"] == "eventClick":
            appt_id = str(action.get("id") or "").strip()
            if appt_id:
                st.session_state[SELECTED_APPT_ID] = appt_id
                st.session_state[LOADED_APPT_ID] = ""  # force prefill
                st.rerun()

        # drag/drop or resize -> move appointment + sync + SMS
        if action["type"] in ("eventDrop", "eventResize"):
            appt_id = str(action.get("id") or "").strip()
            s = parse_iso_any(action.get("start"))
            e = parse_iso_any(action.get("end"))
            if appt_id and s and e:
                s_utc = s.astimezone(timezone.utc) if s.tzinfo else to_utc(s, app_tz)
                e_utc = e.astimezone(timezone.utc) if e.tzinfo else to_utc(e, app_tz)

                appts_all = normalize_appointments(read_csv_df(APPOINTMENTS_CSV, APPT_HEADER))
                appt_old = get_appointment(appts_all, appt_id)

                if not appt_old or appt_old.get("status") != "active":
                    st.warning("Το ραντεβού δεν είναι ενεργό.")
                    st.stop()

                if collision_exists(appts_all, s_utc, e_utc, ignore_appt_id=appt_id):
                    st.warning("⚠️ Επικάλυψη με άλλο ενεργό ραντεβού. Η μετακίνηση ακυρώθηκε.")
                    st.stop()

                # Update appointment times
                appts_all = update_appointment(
                    appts_all,
                    appt_id,
                    {"start_iso": iso_utc(s_utc), "end_iso": iso_utc(e_utc)},
                )
                write_csv_df(APPOINTMENTS_CSV, appts_all, APPT_HEADER)

                # Sync booking/edit panel to the moved appointment
                st.session_state[SELECTED_APPT_ID] = appt_id
                st.session_state[LOADED_APPT_ID] = ""  # force prefill next run

                # Remove queued FUTURE reminders tied to the old time (avoid wrong reminders)
                outbox_delete_queued_for_appt(appt_id, message_types=["reminder_day", "reminder_2h"])

                # Enqueue moved SMS + reschedule reminders (consent-aware)
                appt_new = get_appointment(appts_all, appt_id) or appt_old
                cust = customers[customers["customer_id"] == appt_new.get("customer_id", "")]
                consent = 1 if cust.empty else consent01(cust.iloc[0].get("consent", "1"))

                added = 0
                if consent == 1:
                    added += enqueue_for_moved(appt_new, templates, app_tz)
                    added += reschedule_reminders(appt_new, templates, app_tz)

                if added > 0:
                    send_due_now_feedback()

                st.rerun()

    st.divider()

    def booking_panel() -> None:
        customers2 = normalize_customers(read_csv_df(CUSTOMERS_CSV, CUSTOMER_HEADER))
        if customers2.empty:
            st.info("Πρόσθεσε πελάτη στο tab 🪪 Πελάτες.")
            return

        appts_all = normalize_appointments(read_csv_df(APPOINTMENTS_CSV, APPT_HEADER))

        # ---------------------------
        # AUTO PREFILL (BEFORE WIDGETS)
        # ---------------------------
        selected_appt_id = str(st.session_state.get(SELECTED_APPT_ID, "")).strip()
        if selected_appt_id and st.session_state.get(LOADED_APPT_ID, "") != selected_appt_id:
            appt_sel = get_appointment(appts_all, selected_appt_id)
            if appt_sel:
                st.session_state[FORM_KEYS["customer_id"]] = str(appt_sel.get("customer_id", "")).strip() or st.session_state.get(FORM_KEYS["customer_id"], "")
                st.session_state[FORM_KEYS["service"]] = str(appt_sel.get("service") or "Session")
                st.session_state[FORM_KEYS["notes"]] = str(appt_sel.get("notes") or "")

                s_utc = parse_iso_any(appt_sel.get("start_iso")) or now_utc()
                e_utc = parse_iso_any(appt_sel.get("end_iso")) or (s_utc + timedelta(minutes=30))
                s_loc = to_local(s_utc, app_tz)
                e_loc = to_local(e_utc, app_tz)

                st.session_state[FORM_KEYS["date"]] = s_loc.date()
                st.session_state[FORM_KEYS["time"]] = s_loc.time().replace(second=0, microsecond=0)
                mins = max(15, int((e_loc - s_loc).total_seconds() // 60))
                common = [15, 30, 45, 60, 90, 120]
                st.session_state[FORM_KEYS["duration"]] = min(common, key=lambda x: abs(x - mins))

                st.session_state[FORM_KEYS["pref_new_now"]] = pref_enabled(appt_sel, "pref_send_new_now", default=1) == 1
                st.session_state[FORM_KEYS["pref_day"]] = pref_enabled(appt_sel, "pref_reminder_day", default=0) == 1
                st.session_state[FORM_KEYS["pref_2h"]] = pref_enabled(appt_sel, "pref_reminder_2h", default=0) == 1

                st.session_state[LOADED_APPT_ID] = selected_appt_id

        # build customer maps
        cust_ids = customers2["customer_id"].astype(str).tolist()
        id_to_row: Dict[str, Dict[str, str]] = {str(r["customer_id"]): r.to_dict() for _, r in customers2.iterrows()}  # type: ignore
        id_to_label: Dict[str, str] = {}
        for cid, row in id_to_row.items():
            id_to_label[cid] = f'{row.get("name","")} ({to_e164_heuristic(row.get("phone",""))})'

        cid_now = str(st.session_state.get(FORM_KEYS["customer_id"], "")).strip()
        if cid_now in id_to_label:
            st.session_state[FORM_KEYS["customer_label"]] = id_to_label[cid_now]
        elif cust_ids:
            st.session_state[FORM_KEYS["customer_id"]] = cust_ids[0]
            st.session_state[FORM_KEYS["customer_label"]] = id_to_label.get(cust_ids[0], "")

        selected_appt = get_appointment(appts_all, selected_appt_id) if selected_appt_id else None
        edit_mode = bool(selected_appt and selected_appt.get("status") == "active")

        # Journey-like status row (Portal-style pills)
        pills: List[str] = []
        if edit_mode:
            pills.append(pill("ACTIVE APPOINTMENT", "ok"))
        elif selected_appt and selected_appt.get("status") == "cancelled":
            pills.append(pill("CANCELLED", "bad"))
        else:
            pills.append(pill("NEW APPOINTMENT", "info"))
        pills.append(pill(f"TZ: {os.getenv('APP_TIMEZONE', 'Europe/Athens')}", "muted"))
        pill_row(pills)

        if edit_mode:
            st.info(f"Editing selected appointment: {selected_appt_id}")
        elif selected_appt and selected_appt.get("status") == "cancelled":
            st.warning("Selected appointment is cancelled. Clear selection to create a new one.")
        else:
            st.caption("Create a new appointment (or click an existing one in the calendar to edit).")

        # widgets
        subcard_open("Booking details", icon="🧾")
        c1, c2, c3 = st.columns([2, 1, 1])
        with c1:
            picked_id = st.selectbox(
                "Πελάτης",
                options=cust_ids,
                format_func=lambda x: id_to_label.get(str(x), str(x)),
                key=FORM_KEYS["customer_id"],
            )
            picked_row = id_to_row.get(str(picked_id), {})
            consent = consent01(picked_row.get("consent", "1"))
            st.caption(f"Selected: {id_to_label.get(str(picked_id), '')}")
        with c2:
            st.text_input("Υπηρεσία", key=FORM_KEYS["service"])
        with c3:
            st.selectbox("Διάρκεια (min)", [15, 30, 45, 60, 90, 120], key=FORM_KEYS["duration"])

        d1, d2 = st.columns([1, 1])
        with d1:
            st.date_input("Ημερομηνία (local)", key=FORM_KEYS["date"])
        with d2:
            st.time_input("Ώρα (local)", key=FORM_KEYS["time"])

        st.text_area("Σημειώσεις", height=90, key=FORM_KEYS["notes"])
        subcard_close()

        subcard_open("Messaging options", icon="✉️")
        pill_row([
            pill("Templates: editable", "muted"),
            pill(f"Provider: {effective_provider()}", "muted"),
            pill("Consent: OK", "ok") if consent == 1 else pill("Consent: NO", "bad"),
        ])
        m1, m2, m3 = st.columns([1, 1, 1])
        with m1:
            st.checkbox("Send now (new)", key=FORM_KEYS["pref_new_now"], disabled=(consent == 0))
        with m2:
            st.checkbox("Reminder 1 day before", key=FORM_KEYS["pref_day"], disabled=(consent == 0))
        with m3:
            st.checkbox("Reminder 2 hours before", key=FORM_KEYS["pref_2h"], disabled=(consent == 0))

        if consent == 0:
            st.caption("Messaging is disabled because the selected customer has not given consent.")
        subcard_close()

        # compute start/end from form
        sdate: date = st.session_state[FORM_KEYS["date"]]
        stime: time = st.session_state[FORM_KEYS["time"]]
        duration = int(st.session_state[FORM_KEYS["duration"]])

        start_local = datetime.combine(sdate, stime).replace(tzinfo=app_tz)
        end_local = start_local + timedelta(minutes=duration)
        start_utc = to_utc(start_local, app_tz)
        end_utc = to_utc(end_local, app_tz)

        phone_e = to_e164_heuristic(picked_row.get("phone", ""))

        # new appointment
        if not edit_mode:
            if st.button("✅ Save appointment (new)", width="stretch"):
                appts_all2 = normalize_appointments(read_csv_df(APPOINTMENTS_CSV, APPT_HEADER))
                if collision_exists(appts_all2, start_utc, end_utc):
                    st.warning("⚠️ Επικάλυψη με άλλο ενεργό ραντεβού.")
                    return

                if not valid_phone_e164(phone_e):
                    st.error(f"Invalid phone (need +E.164): '{picked_row.get('phone','')}' -> '{phone_e}'")
                    return

                ts = iso_utc(now_utc())
                appt_id = str(uuid.uuid4())
                appt = {
                    "appointment_id": appt_id,
                    "customer_id": str(picked_row.get("customer_id", "")),
                    "customer_name": str(picked_row.get("name", "")),
                    "customer_phone": phone_e,
                    "start_iso": iso_utc(start_utc),
                    "end_iso": iso_utc(end_utc),
                    "status": "active",
                    "service": (st.session_state.get(FORM_KEYS["service"], "Session") or "Session").strip(),
                    "notes": (st.session_state.get(FORM_KEYS["notes"], "") or "").strip(),
                    "created_at_iso": ts,
                    "updated_at_iso": ts,
                    "pref_send_new_now": "1" if bool(st.session_state.get(FORM_KEYS["pref_new_now"], True)) else "0",
                    "pref_reminder_day": "1" if bool(st.session_state.get(FORM_KEYS["pref_day"], False)) else "0",
                    "pref_reminder_2h": "1" if bool(st.session_state.get(FORM_KEYS["pref_2h"], False)) else "0",
}

                appts_all2 = pd.concat([appts_all2, pd.DataFrame([appt])], ignore_index=True)
                write_csv_df(APPOINTMENTS_CSV, appts_all2, APPT_HEADER)

                added = 0
                if consent == 1:
                    added = enqueue_for_new(appt, templates, app_tz)

                # One-click sending: immediately process any due messages created by this action
                if added > 0:
                    send_due_now_feedback()

                st.success(f"Saved appointment. Outbox added={added}.")
                st.rerun()
            return

        # edit mode buttons
        cbtn1, cbtn2, cbtn3 = st.columns([1, 1, 1])
        with cbtn1:
            update_btn = st.button("🔁 Update (move/edit)", width="stretch")
        with cbtn2:
            cancel_btn = st.button("❌ Cancel appointment", width="stretch")
        with cbtn3:
            clear_btn = st.button("🧹 Clear selection", width="stretch")

        if clear_btn:
            st.session_state[SELECTED_APPT_ID] = ""
            st.session_state[LOADED_APPT_ID] = ""
            st.rerun()

        if update_btn:
            appts_all2 = normalize_appointments(read_csv_df(APPOINTMENTS_CSV, APPT_HEADER))
            if collision_exists(appts_all2, start_utc, end_utc, ignore_appt_id=selected_appt_id):
                st.warning("⚠️ Επικάλυψη με άλλο ενεργό ραντεβού.")
                return

            if not valid_phone_e164(phone_e):
                st.error(f"Invalid phone (need +E.164): '{picked_row.get('phone','')}' -> '{phone_e}'")
                return

            updates = {
                "customer_id": str(picked_row.get("customer_id", "")),
                "customer_name": str(picked_row.get("name", "")),
                "customer_phone": phone_e,
                "start_iso": iso_utc(start_utc),
                "end_iso": iso_utc(end_utc),
                "service": (st.session_state.get(FORM_KEYS["service"], "Session") or "Session").strip(),
                "notes": (st.session_state.get(FORM_KEYS["notes"], "") or "").strip(),
                "pref_send_new_now": "1" if bool(st.session_state.get(FORM_KEYS["pref_new_now"], True)) else "0",
                "pref_reminder_day": "1" if bool(st.session_state.get(FORM_KEYS["pref_day"], False)) else "0",
                "pref_reminder_2h": "1" if bool(st.session_state.get(FORM_KEYS["pref_2h"], False)) else "0",
}
            appts_all2 = update_appointment(appts_all2, selected_appt_id, updates)
            write_csv_df(APPOINTMENTS_CSV, appts_all2, APPT_HEADER)

            appt2 = get_appointment(appts_all2, selected_appt_id) or selected_appt

            outbox_delete_queued_for_appt(selected_appt_id, message_types=["reminder_day", "reminder_2h"])

            added = 0
            if consent == 1 and appt2:
                added += enqueue_for_moved(appt2, templates, app_tz, force=True)
                added += reschedule_reminders(appt2, templates, app_tz)

            st.success(f"Updated appointment. Outbox added={added}.")
            send_due_now_feedback(prefix="One‑click send (moved/edit)")
            st.session_state[LOADED_APPT_ID] = ""
            st.rerun()

        if cancel_btn:
            appts_all2 = normalize_appointments(read_csv_df(APPOINTMENTS_CSV, APPT_HEADER))
            appts_all2 = update_appointment(appts_all2, selected_appt_id, {"status": "cancelled"})
            write_csv_df(APPOINTMENTS_CSV, appts_all2, APPT_HEADER)
            appt2 = get_appointment(appts_all2, selected_appt_id) or selected_appt

            outbox_delete_queued_for_appt(selected_appt_id, message_types=["reminder_day", "reminder_2h", "new", "moved"])

            added = 0
            if consent == 1 and appt2:
                added = enqueue_for_cancelled(appt2, templates, app_tz, force=True)

            st.success(f"Cancelled appointment. Outbox added={added}.")
            send_due_now_feedback(prefix="One‑click send (cancel)")
            st.session_state[SELECTED_APPT_ID] = ""
            st.session_state[LOADED_APPT_ID] = ""
            st.rerun()

    card("Booking / Edit panel", booking_panel, icon="🗓️")



def page_manage_list(templates: Dict[str, str], app_tz: Any) -> None:
    """User-friendly list of current appointments with Move/Cancel + SMS, without changing new-appointment flow."""
    st.subheader("📋 Ραντεβού (List view)")
    st.caption("Επίλεξε ένα ενεργό ραντεβού για μετακίνηση ή ακύρωση. Μετά την ενέργεια, το ημερολόγιο ανανεώνεται αυτόματα.")

    customers = normalize_customers(read_csv_df(CUSTOMERS_CSV, CUSTOMER_HEADER))
    write_csv_df(CUSTOMERS_CSV, customers, CUSTOMER_HEADER)

    appts = normalize_appointments(read_csv_df(APPOINTMENTS_CSV, APPT_HEADER))
    write_csv_df(APPOINTMENTS_CSV, appts, APPT_HEADER)

    active = appts[appts["status"] == "active"].copy()
    if active.empty:
        st.info("Δεν υπάρχουν ενεργά ραντεβού.")
        return

    # Build a readable list (local times)
    rows = []
    for _, r in active.iterrows():
        s_utc = parse_iso_any(r.get("start_iso"))
        e_utc = parse_iso_any(r.get("end_iso"))
        if not s_utc or not e_utc:
            continue
        s_loc = to_local(s_utc, app_tz)
        e_loc = to_local(e_utc, app_tz)
        rows.append({
            "appointment_id": str(r.get("appointment_id","")),
            "start_local": s_loc,
            "end_local": e_loc,
            "when": f"{s_loc.strftime('%Y-%m-%d %H:%M')}–{e_loc.strftime('%H:%M')}",
            "customer": str(r.get("customer_name","")),
            "phone": str(r.get("customer_phone","")),
            "service": str(r.get("service","")),
        })

    dfv = pd.DataFrame(rows)
    if dfv.empty:
        st.info("Δεν υπάρχουν έγκυρα ραντεβού για εμφάνιση.")
        return

    dfv = dfv.sort_values("start_local", ascending=True).reset_index(drop=True)

    labels = [
        f'{row["when"]} • {row["customer"]} • {row["service"]}'
        for _, row in dfv.iterrows()
    ]
    label_to_id = {labels[i]: dfv.iloc[i]["appointment_id"] for i in range(len(labels))}

    default_label = labels[0]
    # If something already selected elsewhere, preselect it here
    sel_id = str(st.session_state.get(SELECTED_APPT_ID, "")).strip()
    if sel_id:
        try:
            idx = dfv.index[dfv["appointment_id"] == sel_id].tolist()
            if idx:
                default_label = labels[idx[0]]
        except Exception:
            pass

    picked_label = st.selectbox("Ενεργά ραντεβού", options=labels, index=labels.index(default_label))
    appt_id = str(label_to_id.get(picked_label, "")).strip()


    # NOTE: Do NOT auto-mutate global selection from this tab on every rerun.
    # Otherwise (because Streamlit executes all tab code), the calendar editor would keep snapping
    # back to the first appointment while the user is trying to create a NEW appointment.
    sync_btn = st.button("🧲 Φόρτωση στο Booking/Edit panel (Calendar tab)", disabled=(not appt_id), key="manage_sync_to_editor")
    if sync_btn and appt_id:
        st.session_state[SELECTED_APPT_ID] = appt_id
        st.session_state[LOADED_APPT_ID] = ""  # force prefill next run
        st.toast("Loaded appointment into editor.", icon="🧲")
        st.rerun()

    appt = get_appointment(appts, appt_id) if appt_id else None
    if not appt:
        st.warning("Δεν βρέθηκε το ραντεβού.")
        return

    s_utc = parse_iso_any(appt.get("start_iso")) or now_utc()
    e_utc = parse_iso_any(appt.get("end_iso")) or (s_utc + timedelta(minutes=30))
    s_loc = to_local(s_utc, app_tz)
    e_loc = to_local(e_utc, app_tz)
    old_duration = max(15, int((e_loc - s_loc).total_seconds() // 60))

    # Consent
    cust = customers[customers["customer_id"] == str(appt.get("customer_id", ""))]
    consent = 1 if cust.empty else consent01(cust.iloc[0].get("consent", "1"))

    def _details_block() -> None:
        pill_row([
            pill("ACTIVE", "ok"),
            pill(appt.get("service", ""), "muted"),
            pill("CONSENT: OK", "ok") if consent == 1 else pill("CONSENT: NO", "bad"),
        ])
        c1, c2, c3 = st.columns([1.3, 1, 1])
        c1.write(f"**Πελάτης:** {appt.get('customer_name','')}  ({appt.get('customer_phone','')})")
        c2.write(f"**Υπηρεσία:** {appt.get('service','')}")
        c3.write(f"**Ώρα:** {s_loc.strftime('%Y-%m-%d %H:%M')} – {e_loc.strftime('%H:%M')}")

    card("Appointment details", _details_block, icon="🧾")

    def _move_block() -> None:
        pill_row([
            pill("MOVE", "info"),
            pill("Will requeue reminders", "muted"),
            pill("One‑click send", "muted"),
        ])
        mc1, mc2, mc3 = st.columns([1, 1, 1])
        with mc1:
            new_date = st.date_input("Νέα ημερομηνία", value=s_loc.date(), key=f"mv_date_{appt_id}")
        with mc2:
            new_time = st.time_input("Νέα ώρα", value=s_loc.time().replace(second=0, microsecond=0), key=f"mv_time_{appt_id}")
        with mc3:
            new_dur = st.selectbox(
                "Νέα διάρκεια (min)",
                [15, 30, 45, 60, 90, 120],
                index=[15, 30, 45, 60, 90, 120].index(min([15, 30, 45, 60, 90, 120], key=lambda x: abs(x - old_duration))),
                key=f"mv_dur_{appt_id}",
            )

        new_start_local = datetime.combine(new_date, new_time).replace(tzinfo=app_tz)
        new_end_local = new_start_local + timedelta(minutes=int(new_dur))
        new_start_utc = to_utc(new_start_local, app_tz)
        new_end_utc = to_utc(new_end_local, app_tz)

        st.caption(f"Old → New: **{s_loc.strftime('%Y-%m-%d %H:%M')}** → **{new_start_local.strftime('%Y-%m-%d %H:%M')}**")

        if st.button("✅ Move appointment + send SMS", width="stretch", key=f"btn_move_{appt_id}"):
            appts_all = normalize_appointments(read_csv_df(APPOINTMENTS_CSV, APPT_HEADER))

            if collision_exists(appts_all, new_start_utc, new_end_utc, ignore_appt_id=appt_id):
                st.warning("⚠️ Επικάλυψη με άλλο ενεργό ραντεβού. Η μετακίνηση ακυρώθηκε.")
            else:
                # Update time
                appts_all = update_appointment(
                    appts_all,
                    appt_id,
                    {"start_iso": iso_utc(new_start_utc), "end_iso": iso_utc(new_end_utc)},
                )
                write_csv_df(APPOINTMENTS_CSV, appts_all, APPT_HEADER)

                # delete future reminders for old schedule, then re-enqueue new ones
                outbox_delete_queued_for_appt(appt_id, message_types=["reminder_day", "reminder_2h"])

                appt2 = get_appointment(appts_all, appt_id) or appt
                added = 0
                if consent == 1 and appt2:
                    added += enqueue_for_moved(appt2, templates, app_tz, force=True)
                    added += reschedule_reminders(appt2, templates, app_tz)

                    # One-click: send immediately (same behavior as new appointments)
                    send_due_now_feedback()

                st.success(f"Moved. Outbox added={added}. Calendar will refresh.")
                st.session_state[SELECTED_APPT_ID] = appt_id
                st.session_state[LOADED_APPT_ID] = ""
                st.rerun()

    card("Move appointment", _move_block, icon="🔁")

    st.divider()

    def _cancel_block() -> None:
        pill_row([
            pill("CANCEL", "bad"),
            pill("Will delete queued reminders", "muted"),
            pill("One‑click send", "muted"),
        ])
        st.caption(f"Old appointment: **{s_loc.strftime('%Y-%m-%d %H:%M')} – {e_loc.strftime('%H:%M')}** → Cancelled")

        if st.button("🛑 Cancel appointment + send SMS", width="stretch", key=f"btn_cancel_{appt_id}"):
            appts_all = normalize_appointments(read_csv_df(APPOINTMENTS_CSV, APPT_HEADER))
            appts_all = update_appointment(appts_all, appt_id, {"status": "cancelled"})
            write_csv_df(APPOINTMENTS_CSV, appts_all, APPT_HEADER)

            # delete any queued future messages for this appointment (avoid wrong reminders)
            outbox_delete_queued_for_appt(appt_id, message_types=["reminder_day", "reminder_2h", "new", "moved"])

            appt2 = get_appointment(appts_all, appt_id) or appt
            added = 0
            if consent == 1 and appt2:
                added += enqueue_for_cancelled(appt2, templates, app_tz, force=True)
                send_due_now_feedback()

            st.success(f"Cancelled. Outbox added={added}. Calendar will refresh.")
            # Clear selection so the panel won't try to edit a cancelled appointment
            st.session_state[SELECTED_APPT_ID] = ""
            st.session_state[LOADED_APPT_ID] = ""
            st.rerun()

    card("Cancel appointment", _cancel_block, icon="❌")


def page_outbox() -> None:
    st.subheader("📨 Αποστολές (Outbox)")
    st.caption(f"Canonical outbox: {OUTBOX_JSONL.resolve()}")
    st.caption(f"Provider log: {PROVIDER_DEBUG_JSONL.resolve()}")
    st.caption(f"SMS_PROVIDER (env): {(os.getenv('SMS_PROVIDER') or '').strip()}")
    st.caption(f"Effective provider (USED): {effective_provider()}")

    df = outbox_state_df()
    if df.empty:
        st.info("Outbox is empty.")
        return

    queued = df[df["status"] == "queued"]
    now = now_utc()
    due_count = 0
    for _, r in queued.iterrows():
        sched = parse_iso_any(r.get("scheduled_for_iso"))
        if sched and sched.astimezone(timezone.utc) <= now:
            due_count += 1

    q_ct = int((df["status"] == "queued").sum())
    f_ct = int((df["status"] == "failed").sum())
    d_ct = int((df["status"] == "deleted").sum())

    def _pipeline_block() -> None:
        pill_row([
            pill(f"Provider: {effective_provider()}", "muted"),
            pill(f"Queued: {q_ct}", "info" if q_ct > 0 else "muted"),
            pill(f"Due now: {due_count}", "warn" if due_count > 0 else "muted"),
            pill(f"Failed: {f_ct}", "bad" if f_ct > 0 else "muted"),
        ])
        c1, c2, c3, c4 = st.columns([1, 1, 1, 1])
        c1.metric("Queued", q_ct)
        c2.metric("Due now", due_count)
        c3.metric("Failed", f_ct)
        c4.metric("Deleted", d_ct)

        if st.button("🚀 Process due now", width="stretch"):
            sent, failed, due_seen = process_due_outbox()
            st.success(f"Processed due={due_seen}: sent={sent}, failed={failed} (provider={effective_provider()})")
            st.rerun()

    card("Messaging pipeline", _pipeline_block, icon="📨")

    st.dataframe(df.sort_values("scheduled_for_iso", ascending=False), width="stretch")


def page_customers() -> None:
    st.subheader("🪪 Πελάτες (Editable)")
    customers = normalize_customers(read_csv_df(CUSTOMERS_CSV, CUSTOMER_HEADER))

    edited = st.data_editor(
        customers,
        width="stretch",
        hide_index=True,
        key="customers_editor",
        column_config={
            "consent": st.column_config.SelectboxColumn("consent", options=["0", "1"], help="1=allow messages, 0=block")
        },
    )

    c1, c2 = st.columns([1, 1])
    with c1:
        if st.button("💾 Save changes", width="stretch"):
            edited2 = normalize_customers(edited.copy())
            write_csv_df(CUSTOMERS_CSV, edited2, CUSTOMER_HEADER)
            st.success("Saved.")
            st.rerun()
    with c2:
        if st.button("➕ Add customer", width="stretch"):
            ts = iso_utc(now_utc())
            row = {
                "customer_id": str(uuid.uuid4()),
                "name": "New Customer",
                "phone": "306900000000",
                "notes": "",
                "consent": "1",
                "created_at_iso": ts,
                "updated_at_iso": ts,
            }
            df = read_csv_df(CUSTOMERS_CSV, CUSTOMER_HEADER)
            df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
            write_csv_df(CUSTOMERS_CSV, df, CUSTOMER_HEADER)
            st.success("Customer added.")
            st.rerun()


def page_templates(templates: Dict[str, str]) -> None:
    st.subheader("✍️ Πρότυπα")
    with st.form("tpl_form"):
        updated: Dict[str, str] = {}
        for k in ["new", "reminder_day", "reminder_2h", "moved", "cancelled"]:
            updated[k] = st.text_area(k, value=templates.get(k, DEFAULT_TEMPLATES[k]), height=120)
        if st.form_submit_button("💾 Save", width="stretch"):
            save_templates(updated)
            st.success("Saved.")
            st.rerun()


def bootstrap() -> None:
    ensure_dirs()
    ensure_csv(APPOINTMENTS_CSV, APPT_HEADER)
    ensure_csv(CUSTOMERS_CSV, CUSTOMER_HEADER)
    if not TEMPLATES_JSON.exists():
        save_templates(dict(DEFAULT_TEMPLATES))
    outbox_jsonl_ensure()


def provider_diagnostics() -> Dict[str, Any]:
    d: Dict[str, Any] = {}
    d["env_SMS_PROVIDER"] = os.getenv("SMS_PROVIDER", "")
    d["effective_provider_used"] = effective_provider()
    raw = os.getenv("INFOBIP_BASE_URL", "")
    d["INFOBIP_BASE_URL_raw"] = raw
    d["INFOBIP_BASE_URL_normalized"] = normalize_infobip_base_url(raw)
    d["INFOBIP_API_KEY_present"] = bool((os.getenv("INFOBIP_API_KEY") or "").strip())
    d["INFOBIP_FROM_present"] = bool((os.getenv("INFOBIP_FROM") or "").strip())
    d["INFOBIP_FROM_value"] = (os.getenv("INFOBIP_FROM") or "").strip()
    return d


def _portal_base_url() -> str:
    """Return the Portal base URL for SaaS/on-prem.

    Priority:
      1) PORTAL_APP_URL
      2) PORTAL_BASE_URL
      3) THERAPY_PORTAL_URL (legacy / docker-compose)
    """
    for k in ("PORTAL_APP_URL", "PORTAL_BASE_URL", "THERAPY_PORTAL_URL"):
        v = (os.getenv(k) or "").strip().rstrip("/")
        if v:
            return v
    return ""


def _fetch_portal_json(path: str, headers: Dict[str, str] | None = None) -> Dict[str, Any] | None:
    """Fetch JSON from the Therapy Portal (server-side).

    This runs inside the SMS container, so it can call the Portal directly.
    We log failures to stdout so Render "Application logs" show what happened.
    """
    base = _portal_base_url()
    if not base:
        print("PORTAL_FETCH: no portal base url set (PORTAL_APP_URL/PORTAL_BASE_URL/THERAPY_PORTAL_URL).", flush=True)
        return None

    url = base + path
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = ""
        print(f"PORTAL_FETCH: HTTPError url={url} status={getattr(e,'code',None)} body={(body or '')[:400]}", flush=True)
        return None
    except Exception as e:
        print(f"PORTAL_FETCH: failed url={url} err={repr(e)}", flush=True)
        return None

def _apply_portal_settings_to_env() -> Dict[str, Any]:
    """Pull clinic + Infobip settings from the portal and apply to this app.

    We support two internal auth mechanisms (both used in this repo):
      - INTERNAL_API_KEY via /api/internal/clinic_settings  (header: X-Internal-Key)
      - INTERNAL_TOKEN  via /api/internal/infobip          (header: x-internal-token)

    We log what we managed to load so you can debug from Render "Application logs".
    """
    out: Dict[str, Any] = {}

    # Clinic display name (non-secret)
    clinic = _fetch_portal_json("/api/clinic_settings") or {}
    if clinic.get("clinic_name"):
        out["clinic_name"] = clinic.get("clinic_name")

    # 1) Preferred: fetch everything from /api/internal/clinic_settings using INTERNAL_API_KEY
    internal_key = (os.getenv("INTERNAL_API_KEY") or "").strip()
    if internal_key:
        data = _fetch_portal_json(f"/api/internal/clinic_settings?tenant={tenant}", headers={"X-Internal-Key": internal_key}) or {}
        # Portal may return either a flat payload or {"clinic": {...}}
        clinic_obj = {}
        if isinstance(data, dict):
            if isinstance(data.get("clinic"), dict):
                clinic_obj = data.get("clinic") or {}
            else:
                clinic_obj = data
        if clinic_obj:
            base_url = str(clinic_obj.get("infobip_base_url") or "").strip()
            sender = str(clinic_obj.get("infobip_sender") or "").strip()
            api_key = str(clinic_obj.get("infobip_api_key") or "").strip()
            if base_url:
                os.environ["INFOBIP_BASE_URL"] = base_url
            if sender:
                os.environ["INFOBIP_FROM"] = sender
                os.environ["INFOBIP_SENDER"] = sender
            if api_key:
                os.environ["INFOBIP_API_KEY"] = api_key

            print(
                "PORTAL_APPLY: clinic_settings loaded "
                f"base_url={'yes' if bool(base_url) else 'no'} "
                f"sender={'yes' if bool(sender) else 'no'} "
                f"api_key={'yes' if bool(api_key) else 'no'}",
                flush=True,
            )
            return out

    # 2) Fallback: /api/internal/infobip using INTERNAL_TOKEN (matches Portal SECRET_KEY)
    tok = (os.getenv("INTERNAL_TOKEN") or "").strip()
    if tok:
        creds = _fetch_portal_json(f"/api/internal/infobip?tenant={tenant}", headers={"x-internal-token": tok}) or {}
        base_url = str(creds.get("infobip_base_url") or "").strip()
        sender = str(creds.get("infobip_sender") or "").strip()
        api_key = str(creds.get("infobip_api_key") or "").strip()
        if base_url:
            os.environ["INFOBIP_BASE_URL"] = base_url
        if sender:
            os.environ["INFOBIP_FROM"] = sender
            os.environ["INFOBIP_SENDER"] = sender
        if api_key:
            os.environ["INFOBIP_API_KEY"] = api_key

        print(
            "PORTAL_APPLY: infobip loaded via /api/internal/infobip "
            f"base_url={'yes' if bool(base_url) else 'no'} "
            f"sender={'yes' if bool(sender) else 'no'} "
            f"api_key={'yes' if bool(api_key) else 'no'}",
            flush=True,
        )
        return out

    print("PORTAL_APPLY: no INTERNAL_API_KEY or INTERNAL_TOKEN set; cannot fetch Infobip creds.", flush=True)
    return out

def main() -> None:
    load_dotenv_simple(
        PROJECT_ROOT / ".env",
        override=False,
        override_prefixes=["SMS_PROVIDER", "INFOBIP_", "APP_TIMEZONE", "CLINIC_"],
    )

    st.set_page_config(page_title="sms3 – Clinic Automation", layout="wide")
    inject_style()

    # Pull per-client settings from the Therapy Portal (clinic name + Infobip creds)
    portal_applied = _apply_portal_settings_to_env()

    # License gating (lightweight offline gate)
    # Tenant-aware license check (no session cookies in Render service-to-service calls)
    internal_key = (os.getenv("INTERNAL_API_KEY") or "").strip()
    lic_headers = {"X-Internal-Key": internal_key} if internal_key else {}
    lic = _fetch_portal_json(f"/api/license?tenant={tenant}", headers=lic_headers) or {}
    if lic:
        pm = str(lic.get("product_mode", "BOTH")).upper()
        if not bool(lic.get("active", True)):
            st.error("This product license/trial has expired. Open Therapy Portal → Clinic Setup → License to renew.")
            st.stop()
        if pm == "PORTAL":
            st.warning("This installation is licensed for Therapy Portal only.")
            st.stop()

    clinic_name = portal_applied.get("clinic_name") or os.getenv("CLINIC_NAME", "Clinic")
    _portal = _portal_base_url()
    portal_suite = f"{_portal}/suite" if _portal else "/suite"
    portal_therapy = f"{_portal}/therapy/" if _portal else "/therapy/"
    st.markdown(
        f"""
<div class="sms-topbar">
  <div>
    <div class="title">📅 SMS Calendar <span style="font-weight:900; opacity:.55;">•</span> {clinic_name}</div>
    <div class="subtitle">Scheduling • reminders • billing & attendance flags</div>
  </div>
  <div class="links">
    <a class="sms-pilllink" href="{portal_suite}" target="_self">🏠 Home</a>
    <a class="sms-pilllink" href="{portal_therapy}" target="_self">🗂️ Therapy Portal</a>
  </div>
</div>
        """,
        unsafe_allow_html=True,
    )

    bootstrap()

    app_tz = get_app_tz()
    templates = load_templates()

    # Cross-app navigation
    # - On-prem Docker gateway: Portal is typically under /therapy/ on the same origin.
    # - SaaS (Render): Portal is on a different origin (set PORTAL_APP_URL or PORTAL_BASE_URL).
    
    _portal = (os.getenv("PORTAL_APP_URL") or os.getenv("PORTAL_BASE_URL") or "").strip().rstrip("/")
    _portal_suite = f"{_portal}/suite" if _portal else "/therapy/suite"
    _portal_home = f"{_portal}/" if _portal else "/therapy/"

    st.sidebar.markdown("### 🔁 Switch apps")
    st.sidebar.markdown(
        f'<a href="{_portal_home}" target="_self" '
        'style="display:block; padding:10px 12px; border-radius:12px; '
        'background:rgba(15,23,42,.06); text-decoration:none; font-weight:700;">'
        '🗂️ Therapy Portal</a>',
        unsafe_allow_html=True,
    )
    st.sidebar.markdown(
        f'<a href="{_portal_suite}" target="_self" '
        'style="display:block; margin-top:8px; padding:10px 12px; border-radius:12px; '
        'background:rgba(15,23,42,.06); text-decoration:none; font-weight:700;">'
        '🏠 Home</a>',
        unsafe_allow_html=True,
    )
    st.sidebar.markdown("---")

    st.sidebar.header("⚙️ Messaging")

    st.sidebar.selectbox(
        "Provider override (USED)",
        options=["(use env)", "mock", "infobip"],
        index=0,
        key=PROVIDER_OVERRIDE_KEY,
        help="Choose infobip to force real sending; (use env) uses SMS_PROVIDER.",
    )

    auto_send = st.sidebar.checkbox("Auto-send due messages (each rerun)", value=True)
    if st.sidebar.button("Process due now", width="stretch"):
        sent, failed, due_seen = process_due_outbox()
        st.sidebar.success(f"Processed due={due_seen}: sent={sent}, failed={failed} (provider={effective_provider()})")
        st.rerun()

    with st.expander("🔧 Diagnostics (paths + provider)", expanded=False):
        st.write("PROJECT_ROOT:", str(PROJECT_ROOT))
        st.write("OUTBOX_JSONL:", str(OUTBOX_JSONL.resolve()), "exists:", OUTBOX_JSONL.exists(), "size:", OUTBOX_JSONL.stat().st_size if OUTBOX_JSONL.exists() else None)
        st.write("PROVIDER_DEBUG_JSONL:", str(PROVIDER_DEBUG_JSONL.resolve()), "exists:", PROVIDER_DEBUG_JSONL.exists(), "size:", PROVIDER_DEBUG_JSONL.stat().st_size if PROVIDER_DEBUG_JSONL.exists() else None)
        st.write("Provider flags:", provider_diagnostics())

    if auto_send:
        sent, failed, due_seen = process_due_outbox()
        if due_seen > 0:
            st.toast(f"Auto-send processed due={due_seen}: sent={sent}, failed={failed} (provider={effective_provider()})", icon="📨")

    tabs = st.tabs(["📅 Ημερολόγιο", "📋 Ραντεβού", "📨 Αποστολές", "🪪 Πελάτες", "✍️ Πρότυπα"])
    with tabs[0]:
        page_calendar(templates, app_tz)
    with tabs[1]:
        page_manage_list(templates, app_tz)
    with tabs[2]:
        page_outbox()
    with tabs[3]:
        page_customers()
    with tabs[4]:
        page_templates(templates)
if __name__ == "__main__":
    main()