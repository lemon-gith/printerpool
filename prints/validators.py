import struct
from pathlib import PurePath

from django.conf import settings
from django.core.exceptions import ValidationError


def validate_model_file(upload):
    ext = PurePath(upload.name).suffix.lower().lstrip(".")
    if ext not in settings.ALLOWED_MODEL_EXTENSIONS:
        allowed = ", ".join(f".{e}" for e in settings.ALLOWED_MODEL_EXTENSIONS)
        raise ValidationError(f"{upload.name}: unsupported file type (allowed: {allowed}).")

    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    if upload.size > max_bytes:
        raise ValidationError(f"{upload.name}: file is larger than {settings.MAX_UPLOAD_MB} MB.")
    if upload.size == 0:
        raise ValidationError(f"{upload.name}: file is empty.")

    upload.seek(0)
    head = upload.read(84)
    upload.seek(0)

    if ext == "3mf":
        if not head.startswith(b"PK\x03\x04"):
            raise ValidationError(f"{upload.name}: not a valid 3MF file.")
    elif ext == "stl" and not _looks_like_stl(head, upload.size):
        raise ValidationError(f"{upload.name}: not a valid STL file.")


def _looks_like_stl(head, size):
    # Binary STL: 80-byte header, uint32 triangle count, then 50 bytes per triangle.
    if len(head) >= 84:
        (triangles,) = struct.unpack("<I", head[80:84])
        if size == 84 + triangles * 50:
            return True
    # ASCII STL. (Binary files may also start with "solid", hence checking binary first.)
    return head.lstrip().lower().startswith(b"solid")
