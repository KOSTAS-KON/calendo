from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List
import pandas as pd

OUTBOX_COLUMNS = [
    "tenant_slug","outbox_id","ts_iso","scheduled_for_iso","to","customer_id","appointment_id",
    "message_type","text","status","confirmed","provider","provider_status",
    "provider_message_id","error","dedupe_key"
]

def _normalize_boolish(v: Any) -> int:
    if v is None:
        return 0
    s = str(v).strip().lower()
    if s in ("1","true","yes","y","on"):
        return 1
    return 0

@dataclass
class OutboxStore:
    path: Path

    def ensure(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(",".join(OUTBOX_COLUMNS) + "\n", encoding="utf-8")

    def load(self) -> pd.DataFrame:
        self.ensure()
        try:
            df = pd.read_csv(self.path, dtype=str).fillna("")
        except Exception:
            # corrupted -> reset but keep file
            self.path.write_text(",".join(OUTBOX_COLUMNS) + "\n", encoding="utf-8")
            df = pd.read_csv(self.path, dtype=str).fillna("")
        for c in OUTBOX_COLUMNS:
            if c not in df.columns:
                df[c] = ""
        # normalize key fields
        df["status"] = df["status"].fillna("").astype(str)
        df["confirmed"] = df["confirmed"].apply(_normalize_boolish).astype(int)
        df = df[OUTBOX_COLUMNS]
        return df

    def save(self, df: pd.DataFrame) -> None:
        self.ensure()
        for c in OUTBOX_COLUMNS:
            if c not in df.columns:
                df[c] = ""
        df = df[OUTBOX_COLUMNS].copy()
        # ensure confirmed is 0/1
        df["confirmed"] = df["confirmed"].apply(_normalize_boolish).astype(int)
        df.to_csv(self.path, index=False, encoding="utf-8")



def store_for_tenant(base_dir: Path, tenant_slug: str) -> OutboxStore:
    """Create an outbox store path that is isolated per-tenant."""
    safe = (tenant_slug or "default").strip().lower()
    safe = "".join(ch for ch in safe if ch.isalnum() or ch in ("-","_")) or "default"
    return OutboxStore(path=base_dir / f"outbox_{safe}.csv")
