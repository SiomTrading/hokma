"""Attachments: storage by content hash, type detection from the bytes, and how each kind reaches the CLI."""
import base64
import email.parser
import email.policy
import hashlib
import os
import re
from pathlib import Path
from typing import Any

from hokma.core.config import hokma_data_dir
from hokma.engine.protocols import AttachmentRecord

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_INLINE_IMAGE_BYTES = 5 * 1024 * 1024        # larger images are refused by the model API: sent as files
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


def attachments_dir() -> Path:
    return hokma_data_dir() / "attachments"


def sniff_mime(data: bytes, declared: str = "") -> str:
    """Image types are decided from the bytes; the declared type is never trusted for them."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if declared.startswith("image/"):
        return "application/octet-stream"
    return declared or "application/octet-stream"


def is_image(mime: str) -> bool:
    return mime in ("image/png", "image/jpeg", "image/gif", "image/webp")


def safe_name(name: str) -> str:
    base = os.path.basename(name.replace("\\", "/")).strip().lstrip(".")
    base = _SAFE_NAME.sub("_", base)[:120].strip()
    return base or "file"


def save_blob(data: bytes) -> str:
    """Store the bytes at <data>/attachments/<sha256>; returns the hash. Same content is stored once."""
    digest = hashlib.sha256(data).hexdigest()
    directory = attachments_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / digest
    if not target.exists():
        tmp = directory / f".{digest}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, target)
    return digest


def parse_multipart(content_type: str, body: bytes) -> tuple[str, str, bytes] | None:
    """(filename, declared type, bytes) of the first file part of a multipart/form-data body."""
    if not content_type.lower().startswith("multipart/"):
        return None
    head = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1", "replace")
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(head + body)
    if not message.is_multipart():
        return None
    for part in message.iter_parts():
        filename = part.get_filename()
        if filename is not None or part.get_param("name", header="content-disposition") == "file":
            payload = part.get_payload(decode=True)
            if payload is None:
                return None
            return filename or "file", part.get_content_type(), payload
    return None


def image_block(record: AttachmentRecord, data: bytes) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": record.mime,
                                        "data": base64.b64encode(data).decode("ascii")}}


def copy_into_project(record: AttachmentRecord, data: bytes, project_dir: Path) -> str:
    """Copy to <project>/attachments/<name>; returns the path relative to the project. Never overwrites."""
    folder = project_dir / "attachments"
    folder.mkdir(parents=True, exist_ok=True)
    name = safe_name(record.name)
    target = folder / name
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != record.sha256:
        stem, dot, ext = name.rpartition(".")
        target = folder / (f"{stem}-{record.sha256[:8]}.{ext}" if dot else f"{name}-{record.sha256[:8]}")
    if not target.exists():
        target.write_bytes(data)
    return f"attachments/{target.name}"
