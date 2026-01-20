from __future__ import annotations

from pathlib import Path

def project_root(start: Path | None = None) -> Path:
    """Return the project root by walking up until we find a folder containing 'apps' and 'src' and 'data'."""
    p = (start or Path(__file__).resolve())
    if p.is_file():
        p = p.parent
    for parent in [p] + list(p.parents):
        if (parent / "apps").exists() and (parent / "src").exists() and (parent / "data").exists():
            return parent
    # fallback: 3 levels up from src/core/paths.py -> project root
    return Path(__file__).resolve().parents[3]

PROJECT_ROOT = project_root()
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = DATA_DIR / "output"
CALENDAR_DIR = DATA_DIR / "calendar"
