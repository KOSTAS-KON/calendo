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

LAST_SELECT_SIG = "__last_calendar_select_sig"
LAST_SELECT_START_UTC = "__last_calendar_select_start_utc"
LAST_SELECT_END_UTC = "__last_calendar_select_end_utc"
LAST_SELECT_LABEL = "__last_calendar_select_local_label"

LAST_CAL_ACTION_SIG = "__last_cal_action_sig"

SELECTED_APPT_ID = "__selected_appointment_id"
LOADED_APPT_ID = "__loaded_appointment_id"

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
    ov_raw = str(st.session_state.get(PROVIDER_OVERRIDE_KEY, "")).strip()
    ov = ov_raw.lower()
    if ov in ("mock", "infobip"):
        return ov

    env_provider = (os.getenv("SMS_PROVIDER") or "").strip().lower()
    if env_provider in ("mock", "infobip"):
        return env_provider

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
    print("SMS_OUTBOX: process_due_outbox called", flush=True)
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

            msgid, status, prov, pstatus = send_via_provider(
                to_e164, str(r.get("body") or ""), channel=str(r.get("channel") or "sms")
            )
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
  --sms-topbar-h: 64px;
}
header[data-testid="stHeader"]{ background: transparent !important; }
.main .block-container{ padding-top: calc(var(--sms-topbar-h) + 18px) !important; }
.stApp { background: var(--bg) !important; color: var(--text) !important; }
.block-container { padding-top: 1.0rem; padding-bottom: 2.2rem; max-width: 1280px; }
section[data-testid="stSidebar"]{ background: var(--panel) !important; border-right: 1px solid var(--border) !important; }
section[data-testid="stSidebar"] .stMarkdown,
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] span{ color: var(--text) !important; }
.stTextInput input, .stTextArea textarea, .stNumberInput input,
.stSelectbox div[data-baseweb="select"] > div,
.stDateInput input{
  border-radius: 14px !important;
  border: 1px solid var(--border) !important;
  background: rgba(255,255,255,.98) !important;
}
.stButton>button{
  border-radius: 14px !important;
  border: 1px solid var(--border-strong) !important;
  background: var(--panel) !important;
  box-shadow: 0 8px 18px rgba(15,23,42,.06) !important;
  font-weight: 800 !important;
}
.stButton>button:hover{ border-color: rgba(15,23,42,.28) !important; }
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
.card {
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 14px 14px;
  margin-bottom: 12px;
  background: var(--panel);
  box-shadow: var(--shadow-soft);
}
.card h3 { margin: 0 0 10px 0; }
.subcard{
  border: 1px dashed rgba(15,23,42,.18);
  border-radius: 16px;
  padding: 12px 12px;
  background: rgba(255,255,255,.86);
  margin: 10px 0 10px 0;
}
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
.sms-topbar .title{ display:flex; align-items:center; gap:10px; font-weight: 900; font-size: 18px; }
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
# Portal fetch helpers
# ----------------------------
def _portal_base_url() -> str:
    for k in ("PORTAL_APP_URL", "PORTAL_BASE_URL", "THERAPY_PORTAL_URL"):
        v = (os.getenv(k) or "").strip().rstrip("/")
        if v:
            return v
    return ""


def _fetch_portal_json(path: str, headers: Dict[str, str] | None = None) -> Dict[str, Any] | None:
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
    out: Dict[str, Any] = {}

    # ✅ FIX: define tenant_slug (previous file used undefined `tenant`)
    tenant_slug = (os.getenv("TENANT_SLUG") or TENANT_SLUG or "default").strip().lower()

    clinic = _fetch_portal_json("/api/clinic_settings") or {}
    if clinic.get("clinic_name"):
        out["clinic_name"] = clinic.get("clinic_name")

    internal_key = (os.getenv("INTERNAL_API_KEY") or "").strip()
    if internal_key:
        data = _fetch_portal_json(
            f"/api/internal/clinic_settings?tenant={tenant_slug}",
            headers={"X-Internal-Key": internal_key},
        ) or {}

        clinic_obj = {}
        if isinstance(data, dict):
            clinic_obj = data.get("clinic") if isinstance(data.get("clinic"), dict) else data

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
                f"tenant={tenant_slug} "
                f"base_url={'yes' if bool(base_url) else 'no'} "
                f"sender={'yes' if bool(sender) else 'no'} "
                f"api_key={'yes' if bool(api_key) else 'no'}",
                flush=True,
            )
            return out

    tok = (os.getenv("INTERNAL_TOKEN") or "").strip()
    if tok:
        creds = _fetch_portal_json(
            f"/api/internal/infobip?tenant={tenant_slug}",
            headers={"x-internal-token": tok},
        ) or {}

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
            f"tenant={tenant_slug} "
            f"base_url={'yes' if bool(base_url) else 'no'} "
            f"sender={'yes' if bool(sender) else 'no'} "
            f"api_key={'yes' if bool(api_key) else 'no'}",
            flush=True,
        )
        return out

    print("PORTAL_APPLY: no INTERNAL_API_KEY or INTERNAL_TOKEN set; cannot fetch Infobip creds.", flush=True)
    return out


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    load_dotenv_simple(
        PROJECT_ROOT / ".env",
        override=False,
        override_prefixes=["SMS_PROVIDER", "INFOBIP_", "APP_TIMEZONE", "CLINIC_"],
    )

    st.set_page_config(page_title="sms3 – Clinic Automation", layout="wide")

    inject_style()

    portal_applied = _apply_portal_settings_to_env()

    # ✅ FIX: tenant-aware license check (uses tenant_slug, not undefined `tenant`)
    tenant_slug = (os.getenv("TENANT_SLUG") or TENANT_SLUG or "default").strip().lower()

    internal_key = (os.getenv("INTERNAL_API_KEY") or "").strip()
    lic_headers = {"X-Internal-Key": internal_key} if internal_key else {}
    lic = _fetch_portal_json(f"/api/license?tenant={tenant_slug}", headers=lic_headers) or {}

    if lic:
        pm = str(lic.get("product_mode", "BOTH")).upper()
        if not bool(lic.get("active", True)):
            st.error("This product license/trial has expired. Open Therapy Portal → Clinic Setup → License to renew.")
            st.stop()
        if pm == "PORTAL":
            st.warning("This installation is licensed for Therapy Portal only.")
            st.stop()

    clinic_name = portal_applied.get("clinic_name") or os.getenv("CLINIC_NAME", "Clinic")

    portal_base = _portal_base_url()
    portal_suite = f"{portal_base}/t/{tenant_slug}/suite" if portal_base else "/suite"
    portal_children = f"{portal_base}/children?tenant={tenant_slug}" if portal_base else "/children"

    st.markdown(
        f"""
<div class="sms-topbar">
  <div>
    <div class="title">📅 SMS Calendar <span style="font-weight:900; opacity:.55;">•</span> {clinic_name}</div>
    <div class="subtitle">Scheduling • reminders • billing & attendance flags</div>
  </div>
  <div class="links">
    <a class="sms-pilllink" href="{portal_suite}" target="_self">🏠 Suite</a>
    <a class="sms-pilllink" href="{portal_children}" target="_self">🧒 Children</a>
  </div>
</div>
        """,
        unsafe_allow_html=True,
    )

    # the rest of the file remains identical to your loaded version
    # (calendar, list view, outbox, customers, templates)
    bootstrap()
    app_tz = get_app_tz()
    templates = load_templates()

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
