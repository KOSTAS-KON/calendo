from pathlib import Path
import pandas as pd

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

def read_csv_safe(path: Path, columns=None):
    """
    Read CSV with NO NaN values (critical for JSON safety).
    """
    if not path.exists():
        if columns is None:
            return pd.DataFrame()
        return pd.DataFrame(columns=columns)

    df = pd.read_csv(
        path,
        dtype=str,
        keep_default_na=False,
        na_filter=False,
    )

    if columns:
        for c in columns:
            if c not in df.columns:
                df[c] = ""

        df = df[columns]

    return df


def write_csv_safe(df: pd.DataFrame, path: Path):
    """
    Write CSV ensuring all values are strings (no NaN).
    """
    for col in df.columns:
        df[col] = df[col].astype(str).fillna("")

    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
