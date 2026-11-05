"""Accepting and serving uploaded files (stored in the database)."""
import os
from urllib.parse import quote

from flask import Response
from flask_login import current_user

from .models import StoredFile

ALLOWED = {
    ".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".heic": "image/heic", ".webp": "image/webp",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
}
MAX_BYTES = 4 * 1024 * 1024
ACCEPT = ",".join(ALLOWED)


def take_upload(file, errors, required=True):
    """Validate an uploaded werkzeug FileStorage and return an unsaved StoredFile (or None)."""
    if not file or not file.filename:
        if required:
            errors.append("Attach a file.")
        return None
    name = os.path.basename(file.filename.replace("\\", "/")).strip() or "file"
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED:
        errors.append("Files must be PDF, an image (JPG, PNG, HEIC, WebP), Word, Excel or text.")
        return None
    data = file.read()
    if not data:
        errors.append("That file is empty.")
        return None
    if len(data) > MAX_BYTES:
        errors.append("Files can be at most 4 MB.")
        return None
    return StoredFile(filename=name[:255], content_type=ALLOWED[ext], size=len(data), data=data,
                      uploaded_by_id=current_user.id if current_user.is_authenticated else None)


def send(stored):
    """Serve a file inline (PDFs and images open in the browser)."""
    inline = stored.content_type.startswith(("application/pdf", "image/", "text/"))
    disposition = "inline" if inline else "attachment"
    return Response(stored.data, mimetype=stored.content_type, headers={
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(stored.filename)}",
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


def human_size(n):
    for unit in ("bytes", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
