# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import re
import uuid
import urllib.request
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
        st.error("Security is not configured: missing SSO_SHARED_SECRET.")
        st.stop()

    max_age = int(os.getenv("SSO_MAX_AGE_SECONDS") or "900")
    ser = URLSafeTimedSerializer(secret_key=secret, salt="calendo-sms-sso-v1")

    if not sso:
        st.warning("Open SMS Calendar from Therapy Portal (missing access token).")
        st.stop()

    try:
        payload = ser.loads(sso, max_age=max_age)
    except SignatureExpired:
        st.warning("Access link expired. Open again from Therapy Portal.")
        st.stop()
    except BadSignature:
        st.error("Invalid access token. Open again from Therapy Portal.")
        st.stop()
    except Exception:
        st.error("Access token validation failed.")
        st.stop()

    token_tenant = str((payload or {}).get("tenant") or "").strip().lower()
    if token_tenant and token_tenant != tenant:
        st.error("Tenant mismatch. Open again from Therapy Portal.")
        st.stop()

    st.session_state["_sso_ok"] = True
    st.session_state["_tenant"] = tenant
    return tenant, True


TENANT_SLUG, _ = _require_sso()


# =============================================================================
# PATHS (Outbox is local; appointments are in Portal)
# =============================================================================
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = DATA_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTBOX_JSONL = OUTPUT_DIR / "outbox.jsonl"

DEFAULT_TEMPLATES: Dict[str, str] = {
    "new": "New appointment for {name} on {date} at {time}.",
    "reminder_day": "Reminder: {name} has an appointment tomorrow ({date}) at {time}.",
    "reminder_2h": "Reminder: {name} has an appointment at {time} (in ~2 hours).",
    "moved": "Update: {name}'s appointment moved to {date} {time}.",
    "cancelled": "Cancelled: {name}'s appointment on {date} {time} was cancelled.",
}


# =============================================================================
# Helpers
# =============================================================================
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
    return re.sub(r"[^\d+]", "", str(phone).strip())


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


def render_tpl(tpl: str, name: str, start_local: datetime) -> str:
    return (
        (tpl or "")
        .replace("{name}", name or "")
        .replace("{date}", start_local.strftime("%Y-%m-%d"))
        .replace("{time}", start_local.strftime("%H:%M"))
    )


# =============================================================================
# Portal API helpers
# =============================================================================
def _portal_base_url() -> str:
    for k in ("PORTAL_APP_URL", "PORTAL_BASE_URL", "THERAPY_PORTAL_URL", "PORTAL_URL"):
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


def portal_get_children(tenant_slug: str, internal_key: str) -> list[dict]:
    data = _fetch_portal_json(
        f"/api/internal/children?tenant={tenant_slug}",
        headers={"X-Internal-Key": internal_key},
    ) or {}
    return list((data.get("children") or [])) if isinstance(data, dict) else []


def portal_get_appointments(tenant_slug: str, internal_key: str, days: int = 60) -> list[dict]:
    data = _fetch_portal_json(
        f"/api/internal/appointments?tenant={tenant_slug}&days={days}",
        headers={"X-Internal-Key": internal_key},
    ) or {}
    return list((data.get("appointments") or [])) if isinstance(data, dict) else []


def portal_create_appointment(
    tenant_slug: str,
    internal_key: str,
    child_id: int,
    starts_at_iso: str,
    ends_at_iso: str,
    therapist_name: str,
    procedure: str,
) -> Optional[int]:
    base = _portal_base_url()
    if not base:
        return None
    url = base + "/api/internal/appointments/create"
    try:
        r = requests.post(
            url,
            headers={"X-Internal-Key": internal_key},
            data={
                "tenant": tenant_slug,
                "child_id": str(child_id),
                "starts_at_iso": starts_at_iso,
                "ends_at_iso": ends_at_iso,
                "therapist_name": therapist_name,
                "procedure": procedure,
            },
            timeout=10,
        )
        if r.status_code != 200:
            return None
        j = r.json()
        return int(j.get("appointment_id"))
    except Exception:
        return None


def portal_move_appointment(tenant_slug: str, internal_key: str, appointment_id: int, starts_at_iso: str, ends_at_iso: str) -> bool:
    base = _portal_base_url()
    if not base:
        return False
    url = base + f"/api/internal/appointments/{appointment_id}/move"
    try:
        r = requests.post(
            url,
            headers={"X-Internal-Key": internal_key},
            data={"tenant": tenant_slug, "starts_at_iso": starts_at_iso, "ends_at_iso": ends_at_iso},
            timeout=10,
        )
        return r.status_code == 200
    except Exception:
        return False


def portal_cancel_appointment(tenant_slug: str, internal_key: str, appointment_id: int) -> bool:
    base = _portal_base_url()
    if not base:
        return False
    url = base + f"/api/internal/appointments/{appointment_id}/cancel"
    try:
        r = requests.post(url, headers={"X-Internal-Key": internal_key}, data={"tenant": tenant_slug}, timeout=10)
        return r.status_code == 200
    except Exception:
        return False


# =============================================================================
# Outbox (local)
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
    if not OUTBOX_JSONL.exists():
        OUTBOX_JSONL.write_text("", encoding="utf-8")


def outbox_jsonl_append(obj: Dict[str, Any]) -> None:
    outbox_jsonl_ensure()
    with OUTBOX_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def outbox_state_df() -> pd.DataFrame:
    outbox_jsonl_ensure()
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

    if not latest:
        return pd.DataFrame(columns=OUTBOX_COLUMNS)

    rows = []
    for oid, r in latest.items():
        rows.append({
            "outbox_id": oid,
            "scheduled_for_iso": r.get("scheduled_for_iso",""),
            "appointment_id": r.get("appointment_id",""),
            "to": r.get("to",""),
            "message_type": r.get("message_type",""),
            "body": r.get("body",""),
            "status": r.get("status",""),
            "provider": r.get("provider",""),
            "provider_msgid": r.get("provider_msgid",""),
            "provider_status": r.get("provider_status",""),
            "error": r.get("error",""),
            "dedupe_key": r.get("dedupe_key",""),
        })
    df = pd.DataFrame(rows)
    for c in OUTBOX_COLUMNS:
        if c not in df.columns:
            df[c] = ""
    return df[OUTBOX_COLUMNS]


def _dedupe_key(appt_id: str, message_type: str, when_iso: str) -> str:
    return f"{appt_id}|{message_type}|{when_iso}"


def outbox_enqueue(appointment_id: str, to_phone: str, body: str, message_type: str, when_utc: datetime) -> bool:
    when_iso = iso_utc(when_utc)
    dk = _dedupe_key(str(appointment_id), message_type, when_iso)
    df = outbox_state_df()
    if not df.empty and (df["dedupe_key"] == dk).any():
        return False
    outbox_jsonl_append({
        "outbox_id": str(uuid.uuid4()),
        "ts_iso": iso_utc(now_utc()),
        "scheduled_for_iso": when_iso,
        "appointment_id": str(appointment_id),
        "to": to_e164_heuristic(to_phone),
        "message_type": message_type,
        "body": body,
        "status": "queued",
        "provider": "",
        "provider_msgid": "",
        "provider_status": "",
        "error": "",
        "dedupe_key": dk,
    })
    return True


def outbox_mark(outbox_id: str, *, status: str, provider: str, provider_msgid: str = "", provider_status: str = "", error: str = "") -> None:
    outbox_jsonl_append({
        "outbox_id": outbox_id,
        "ts_iso": iso_utc(now_utc()),
        "status": status,
        "provider": provider,
        "provider_msgid": provider_msgid,
        "provider_status": provider_status,
        "error": error,
    })


def outbox_delete(outbox_id: str) -> None:
    outbox_mark(outbox_id, status="deleted", provider="user", error="deleted_by_user")


# =============================================================================
# Sending (Infobip/mock)
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
        raise RuntimeError("Infobip not configured")
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
    return "mock_" + uuid.uuid4().hex[:12], "MOCK"


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


def _send_immediately() -> None:
    sent, failed, due = process_due_outbox(limit=50)
    if due == 0:
        st.info("Nothing due to send.")
    elif failed == 0:
        st.success(f"Sent {sent} SMS.")
    else:
        st.warning(f"Processed due={due}: sent={sent}, failed={failed}. Check Outbox error.")


# =============================================================================
# Pages
# =============================================================================
def _topbar(tenant_slug: str) -> None:
    st.markdown(
        """
<style>
.main .block-container{ max-width: 1200px; }
.topbar{
  display:flex; justify-content:space-between; align-items:center; gap:12px;
  padding: 10px 12px; border:1px solid rgba(0,0,0,.08); border-radius:14px;
  background:#fff; margin-bottom:12px;
}
.topbar .title{ font-weight: 900; font-size: 22px; letter-spacing:-0.02em; }
.topbar .links a{
  text-decoration:none; border:1px solid rgba(0,0,0,.12);
  padding:8px 12px; border-radius:999px; background:#fff; font-weight:800;
}
</style>
        """,
        unsafe_allow_html=True,
    )
    portal = _portal_base_url().rstrip("/")
    if portal:
        st.markdown(
            f"""
<div class="topbar">
  <div class="title">📅 SMS Calendar (Portal-synced) — tenant: <span style="color:#0f172a">{tenant_slug}</span></div>
  <div class="links">
    <a href="{portal}/t/{tenant_slug}/suite">🏠 Suite</a>
    <a href="{portal}/children?tenant={tenant_slug}">🧒 Children</a>
  </div>
</div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.title(f"SMS Calendar (Portal-synced) — tenant: {tenant_slug}")


def page_calendar(templates: Dict[str, str], tz: Any, tenant_slug: str, internal_key: str) -> None:
    st.subheader("Calendar")

    view = st.selectbox(
        "View",
        options=[("Month", "dayGridMonth"), ("Week", "timeGridWeek"), ("Day", "timeGridDay")],
        format_func=lambda x: x[0],
        key="cal_view",
    )[1]

    children = portal_get_children(tenant_slug, internal_key) if internal_key else []
    appts = portal_get_appointments(tenant_slug, internal_key, days=60) if internal_key else []

    with st.expander("➕ New appointment (creates in Portal)", expanded=False):
        if not children:
            st.warning("No children found in Portal for this tenant.")
        else:
            child_map = {f"{c['full_name']} (#{c['id']})": c for c in children}
            child_label = st.selectbox("Child", list(child_map.keys()), key="new_child")
            procedure = st.text_input("Procedure", value="Session", key="new_proc")
            therapist = st.text_input("Therapist", value="", key="new_ther")
            start_local = st.datetime_input("Start (local)", value=datetime.now(tz).replace(second=0, microsecond=0) + timedelta(hours=1), key="new_start")
            duration = st.number_input("Duration (min)", min_value=15, max_value=240, value=45, step=15, key="new_dur")

            c1, c2, c3 = st.columns(3)
            with c1:
                send_now = st.checkbox("Send SMS now", value=True, key="new_send_now")
            with c2:
                rem_day = st.checkbox("Reminder 24h before", value=False, key="new_rem_day")
            with c3:
                rem_2h = st.checkbox("Reminder 2h before", value=False, key="new_rem_2h")

            if st.button("Create in Portal", key="create_portal_btn"):
                child = child_map[child_label]
                start_utc = to_utc(start_local, tz)
                end_utc = start_utc + timedelta(minutes=int(duration))

                appt_id = portal_create_appointment(
                    tenant_slug, internal_key, int(child["id"]),
                    iso_utc(start_utc), iso_utc(end_utc),
                    therapist, procedure
                )
                if not appt_id:
                    st.error("Failed to create appointment in Portal.")
                else:
                    to_phone = child.get("parent1_phone") or child.get("parent2_phone") or ""
                    if send_now:
                        body = render_tpl(templates["new"], child["full_name"], start_local)
                        outbox_enqueue(str(appt_id), to_phone, body, "new", now_utc())
                        _send_immediately()

                    if rem_day:
                        when = start_utc - timedelta(hours=24)
                        if when > now_utc():
                            body = render_tpl(templates["reminder_day"], child["full_name"], start_local)
                            outbox_enqueue(str(appt_id), to_phone, body, "reminder_day", when)

                    if rem_2h:
                        when = start_utc - timedelta(hours=2)
                        if when > now_utc():
                            body = render_tpl(templates["reminder_2h"], child["full_name"], start_local)
                            outbox_enqueue(str(appt_id), to_phone, body, "reminder_2h", when)

                    st.success("Created in Portal. Calendar will refresh.")
                    st.rerun()

    if st_calendar is None:
        st.info("streamlit_calendar not installed; use Appointments tab.")
        return

    events = []
    for a in appts:
        title = f"{a.get('child_name','')} • {a.get('procedure','Session')}"
        if a.get("status") == "cancelled":
            title = "❌ " + title
        events.append({"id": str(a["id"]), "title": title, "start": a["starts_at"], "end": a["ends_at"], "editable": a.get("status") != "cancelled"})

    cal = st_calendar({
        "initialView": view,
        "height": 720,
        "headerToolbar": {"left": "prev,next today", "center": "title", "right": "dayGridMonth,timeGridWeek,timeGridDay"},
        "editable": True,
        "eventDurationEditable": True,
        "eventStartEditable": True,
        "events": events,
    })

    if cal and isinstance(cal, dict):
        if cal.get("eventDrop"):
            ev = cal["eventDrop"]["event"]
            ok = portal_move_appointment(tenant_slug, internal_key, int(ev["id"]), ev["start"], ev.get("end") or ev["start"])
            if ok:
                st.success("Moved in Portal.")
                st.rerun()
            else:
                st.error("Move failed.")
        if cal.get("eventResize"):
            ev = cal["eventResize"]["event"]
            ok = portal_move_appointment(tenant_slug, internal_key, int(ev["id"]), ev["start"], ev.get("end") or ev["start"])
            if ok:
                st.success("Resized in Portal.")
                st.rerun()
            else:
                st.error("Resize failed.")


def page_appointments(templates: Dict[str, str], tz: Any, tenant_slug: str, internal_key: str) -> None:
    st.subheader("Appointments (Portal)")

    appts = portal_get_appointments(tenant_slug, internal_key, days=60) if internal_key else []
    if not appts:
        st.info("No appointments found.")
        return

    df = pd.DataFrame(appts)
    st.dataframe(df, width="stretch")

    appt_ids = [str(a["id"]) for a in appts]
    appt_id = st.selectbox("Appointment", appt_ids, key="appt_pick")
    a = next(x for x in appts if str(x["id"]) == appt_id)

    start_utc = parse_iso_any(a["starts_at"]) or now_utc()
    start_local = to_local(start_utc, tz)

    st.markdown("### One-click send now (immediate)")
    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("📨 NEW now", key="btn_new_now"):
            body = render_tpl(templates["new"], a["child_name"], start_local)
            outbox_enqueue(appt_id, a.get("to_phone",""), body, "new", now_utc()); _send_immediately()
    with c2:
        if st.button("🔁 MOVED now", key="btn_moved_now"):
            body = render_tpl(templates["moved"], a["child_name"], start_local)
            outbox_enqueue(appt_id, a.get("to_phone",""), body, "moved", now_utc()); _send_immediately()
    with c3:
        if st.button("❌ CANCELLED now", key="btn_cancel_now"):
            body = render_tpl(templates["cancelled"], a["child_name"], start_local)
            outbox_enqueue(appt_id, a.get("to_phone",""), body, "cancelled", now_utc()); _send_immediately()

    st.markdown("### Move / cancel in Portal")
    new_start_local = st.datetime_input("New start (local)", value=start_local, key="mv_start")
    dur = st.number_input("Duration (min)", min_value=15, max_value=240, value=45, step=15, key="mv_dur")
    if st.button("Apply move", key="mv_apply"):
        ns = to_utc(new_start_local, tz)
        ne = ns + timedelta(minutes=int(dur))
        ok = portal_move_appointment(tenant_slug, internal_key, int(appt_id), iso_utc(ns), iso_utc(ne))
        if ok:
            st.success("Moved in Portal.")
            st.rerun()
        else:
            st.error("Move failed.")
    if st.button("Cancel appointment", key="mv_cancel"):
        ok = portal_cancel_appointment(tenant_slug, internal_key, int(appt_id))
        if ok:
            st.success("Cancelled in Portal.")
            st.rerun()
        else:
            st.error("Cancel failed.")


def page_outbox() -> None:
    st.subheader("Outbox")
    df = outbox_state_df()
    if df.empty:
        st.info("Outbox is empty.")
        return

    c1, c2 = st.columns([1, 2])
    with c1:
        if st.button("Process due now", key="proc_due"):
            sent, failed, due = process_due_outbox()
            st.success(f"Processed due={due}: sent={sent}, failed={failed}.")

    st.dataframe(df.sort_values(by="scheduled_for_iso", ascending=False), width="stretch")

    queued = df[df["status"] == "queued"].copy()
    if queued.empty:
        return
    pick = st.selectbox("Delete queued", [f"{r['outbox_id']} | {r['message_type']} | {r['scheduled_for_iso']}" for _, r in queued.iterrows()], key="del_pick")
    if st.button("🗑 Delete", key="del_btn"):
        oid = pick.split("|")[0].strip()
        outbox_delete(oid)
        st.success("Deleted (marked).")
        st.rerun()


def page_customers() -> None:
    st.subheader("Customers")
    st.info("Portal-synced mode uses Portal Children. This tab is optional.")


def page_templates(templates: Dict[str, str]) -> None:
    st.subheader("Templates")
    tpls = dict(templates)
    tpls["new"] = st.text_area("New template", value=tpls.get("new",""), height=80, key="tpl_new")
    tpls["reminder_day"] = st.text_area("Reminder 24h template", value=tpls.get("reminder_day",""), height=70, key="tpl_day")
    tpls["reminder_2h"] = st.text_area("Reminder 2h template", value=tpls.get("reminder_2h",""), height=70, key="tpl_2h")
    tpls["moved"] = st.text_area("Moved template", value=tpls.get("moved",""), height=70, key="tpl_moved")
    tpls["cancelled"] = st.text_area("Cancelled template", value=tpls.get("cancelled",""), height=70, key="tpl_cancelled")
    if st.button("Save templates", key="tpl_save_btn"):
        st.success("Saved (session only).")


def page_diagnostics(tenant_slug: str, internal_key: str) -> None:
    st.subheader("Diagnostics")
    st.json({
        "tenant": tenant_slug,
        "portal_base": _portal_base_url(),
        "provider": _effective_provider(),
        "INFOBIP_BASE_URL_set": bool((os.getenv("INFOBIP_BASE_URL") or "").strip()),
        "INFOBIP_API_KEY_set": bool((os.getenv("INFOBIP_API_KEY") or "").strip()),
        "INFOBIP_FROM_set": bool((os.getenv("INFOBIP_FROM") or "").strip()),
        "internal_key_set": bool(internal_key),
        "note": "For auto-send every 5 min use Render Cron Job: python sms/tools/run_outbox_once.py",
    })


# =============================================================================
# MAIN
# =============================================================================
def main() -> None:
    st.set_page_config(page_title="Calendo SMS", layout="wide")

    tenant_slug = (os.getenv("TENANT_SLUG") or TENANT_SLUG or "default").strip().lower()
    internal_key = (os.getenv("INTERNAL_API_KEY") or "").strip()
    tz = get_app_tz()
    templates = DEFAULT_TEMPLATES

    _topbar(tenant_slug)

    if not internal_key:
        st.error("INTERNAL_API_KEY missing on SMS service. Cannot sync with Portal.")
        st.stop()

    tabs = st.tabs(["📅 Calendar", "📋 Appointments", "📨 Outbox", "🧑 Customers", "✍️ Templates", "🛠 Diagnostics"])
    with tabs[0]:
        page_calendar(templates, tz, tenant_slug, internal_key)
    with tabs[1]:
        page_appointments(templates, tz, tenant_slug, internal_key)
    with tabs[2]:
        page_outbox()
    with tabs[3]:
        page_customers()
    with tabs[4]:
        page_templates(templates)
    with tabs[5]:
        page_diagnostics(tenant_slug, internal_key)


if __name__ == "__main__":
    main()
