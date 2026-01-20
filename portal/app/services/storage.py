from pathlib import Path
from uuid import uuid4
from fastapi import UploadFile
from app.models.attachment import Attachment

UPLOAD_ROOT = Path("storage/uploads")
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

def save_upload(child_id: int, upload: UploadFile, session_note_id: int | None = None) -> Attachment:
    suffix = Path(upload.filename or "").suffix.lower()
    safe_name = f"{uuid4().hex}{suffix}"
    dest = UPLOAD_ROOT / str(child_id)
    dest.mkdir(parents=True, exist_ok=True)

    file_path = dest / safe_name
    with file_path.open("wb") as f:
        f.write(upload.file.read())

    return Attachment(
        child_id=child_id,
        session_note_id=session_note_id,
        original_name=upload.filename or "upload",
        mime_type=upload.content_type or "application/octet-stream",
        storage_path=str(file_path),
    )

def delete_file(storage_path: str) -> None:
    try:
        p = Path(storage_path)
        if p.exists():
            p.unlink()
    except Exception:
        pass
