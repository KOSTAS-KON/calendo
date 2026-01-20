import argparse
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

from .store_csv import ensure_files, load_customers, load_appts, append_outbox


@dataclass(frozen=True)
class ReminderMode:
    name: str
    offset_minutes: int
    window_minutes: int
    template: str


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _now(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)


def _fmt_when(dt: datetime) -> str:
    # Greek-friendly, short
    return dt.strftime("%a %d/%m %H:%M")


def _dedupe_key(appointment_id: str, mode_name: str, send_at_iso: str) -> str:
    raw = f"{appointment_id}|{mode_name}|{send_at_iso}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _read_outbox(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype=str)
    except Exception:
        return pd.DataFrame()


def _already_queued(outbox: pd.DataFrame, dedupe: str) -> bool:
    if outbox.empty:
        return False
    if "dedupe_key" not in outbox.columns:
        return False
    return bool((outbox["dedupe_key"].astype(str) == str(dedupe)).any())


def queue_due_reminders(
    now: datetime,
    tz: ZoneInfo,
    modes: list[ReminderMode],
    send_after_queue: bool,
) -> dict:
    ensure_files()

    customers = load_customers()
    appts = load_appts()

    outbox_path = Path("data/calendar/outbox.csv")
    outbox = _read_outbox(outbox_path)

    # Basic validation
    if customers.empty or appts.empty:
        return {"queued": 0, "skipped": 0, "reason": "no customers or appointments"}

    # Map customers
    cust_map = {
        str(r["customer_id"]): {"name": str(r["name"]), "phone": str(r["phone"])}
        for _, r in customers.iterrows()
    }

    queued = 0
    skipped = 0

    # Only scheduled appointments
    appts = appts.copy()
    appts["status"] = appts.get("status", "scheduled")
    appts = appts[appts["status"].astype(str) == "scheduled"].copy()

    for _, a in appts.iterrows():
        appt_id = str(a["appointment_id"])
        cust_id = str(a["customer_id"])
        start_iso = str(a["start_iso"])
        try:
            start_dt = datetime.fromisoformat(start_iso).astimezone(tz)
        except Exception:
            skipped += 1
            continue

        cust = cust_map.get(cust_id)
        if not cust:
            skipped += 1
            continue

        for mode in modes:
            send_at = start_dt - timedelta(minutes=mode.offset_minutes)

            # If send_at is within +/- window_minutes of "now" -> due
            window = timedelta(minutes=mode.window_minutes)
            if not (send_at - window <= now <= send_at + window):
                continue

            dedupe = _dedupe_key(appt_id, mode.name, send_at.isoformat())
            if _already_queued(outbox, dedupe):
                skipped += 1
                continue

            text = mode.template.format(name=cust["name"], when=_fmt_when(start_dt))

            # Append to outbox.csv via existing helper.
            # We store dedupe_key by writing it into the freeform "error" column if schema lacks it,
            # BUT better is to add a real column. We'll try to add it if possible below.
            append_outbox(
                to=cust["phone"],
                customer_id=cust_id,
                appointment_id=appt_id,
                message_type=mode.name,
                text=text,
            )
            queued += 1

            # Add dedupe_key into outbox.csv if the column exists, else add it.
            try:
                outbox = _read_outbox(outbox_path)
                if "dedupe_key" not in outbox.columns:
                    outbox["dedupe_key"] = ""
                # last row assumed the one we just appended
                outbox.loc[outbox.index.max(), "dedupe_key"] = dedupe
                outbox.to_csv(outbox_path, index=False, encoding="utf-8")
            except Exception:
                pass

    result = {"queued": queued, "skipped": skipped, "now": now.isoformat()}

    if send_after_queue and queued > 0:
        # Import lazily (keeps scheduler usable even if provider env vars missing)
        from .send_outbox import main as send_main

        send_main()
        result["sent_after_queue"] = True

    return result


def main():
    parser = argparse.ArgumentParser(description="Queue appointment reminders (CSV-based).")
    parser.add_argument("--config", default="config/scheduler.yaml", help="Path to scheduler yaml")
    parser.add_argument("--queue-only", action="store_true", help="Only queue reminders (do not send)")
    parser.add_argument("--queue-and-send", action="store_true", help="Queue reminders and then send immediately")
    args = parser.parse_args()

    cfg = _load_yaml(Path(args.config))
    tz = ZoneInfo(cfg.get("timezone", "Europe/Athens"))

    rem = cfg.get("reminders", {}) or {}
    if not rem.get("enabled", True):
        print({"queued": 0, "skipped": 0, "reason": "reminders disabled"})
        return

    modes_cfg = rem.get("modes", []) or []
    modes: list[ReminderMode] = []
    for m in modes_cfg:
        try:
            modes.append(
                ReminderMode(
                    name=str(m["name"]),
                    offset_minutes=int(m["offset_minutes"]),
                    window_minutes=int(m.get("window_minutes", 10)),
                    template=str(m.get("template", "Reminder: {when}")),
                )
            )
        except Exception:
            continue

    sending = cfg.get("sending", {}) or {}
    send_after_queue = bool(sending.get("send_after_queue", False))

    # CLI override
    if args.queue_only:
        send_after_queue = False
    if args.queue_and_send:
        send_after_queue = True

    res = queue_due_reminders(_now(tz), tz, modes, send_after_queue=send_after_queue)
    print(res)


if __name__ == "__main__":
    main()
