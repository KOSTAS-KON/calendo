from __future__ import annotations

from pathlib import Path

import pandas as pd


def mark_outbox_sent(ids: list[str] | None = None) -> int:
    """Mark queued outbox rows as sent (manual testing utility). If ids provided, mark only those outbox_id values."""
    path = Path("data/calendar/outbox.csv")
    if not path.exists():
        return 0
    df = pd.read_csv(path, dtype=str)
    if df.empty:
        return 0

    for col, default in [
        ("status", ""),
        ("provider", ""),
        ("provider_status", ""),
        ("provider_message_id", ""),
        ("error", ""),
    ]:
        if col not in df.columns:
            df[col] = default

    mask = df["status"].astype(str) == "queued"
    if ids:
        ids_set = set(str(x) for x in ids)
        mask = mask & df["outbox_id"].astype(str).isin(ids_set)

    n = int(mask.sum())
    if n == 0:
        return 0

    df.loc[mask, "status"] = "sent"
    df.loc[mask, "provider"] = "manual"
    df.loc[mask, "provider_status"] = "SENT_DEMO"
    df.loc[mask, "provider_message_id"] = ""
    df.loc[mask, "error"] = ""

    df.to_csv(path, index=False, encoding="utf-8")
    return n
