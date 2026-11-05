"""Path rules: everything a request names is relative to the project folder and is checked before use.

Reading and writing go through `hokma.kit.safefs`, which walks the path one component at a time without
following symlinks, so a link planted inside the project cannot lead outside of it.
"""
import os
import re
import stat
from pathlib import Path

from hokma.engine.attachments import safe_name
from hokma.projects.errors import ProjectError

_BAD = re.compile(r"[\x00-\x1f]")

IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "heic", "svg"}
AUDIO_EXT = {"mp3", "m4a", "wav", "ogg", "flac", "aac", "opus", "webm"}
CODE_EXT = {"py", "ipynb", "js", "ts", "tsx", "jsx", "cs", "pine", "sql", "sh", "r", "cpp", "c", "h", "java", "go",
            "rs", "yaml", "yml", "toml"}
DOC_EXT = {"pdf", "doc", "docx", "txt", "md", "csv", "xls", "xlsx", "ppt", "pptx", "rtf", "odt", "ods", "json"}


def clean_rel(rel: object) -> str:
    """Normalized POSIX relative path, or ProjectError. No absolute paths, no `..`, no backslashes or controls."""
    if not isinstance(rel, str) or not rel.strip() or len(rel) > 400 or _BAD.search(rel) or "\\" in rel:
        raise ProjectError("bad_path", 400)
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts) or re.match(r"^[A-Za-z]:", rel) or parts[0].lower() == ".git":
        raise ProjectError("bad_path", 400)
    return "/".join(parts)


def subfolder_for(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in IMAGE_EXT:
        return "imagenes"
    if ext in AUDIO_EXT:
        return "audios"
    if ext in CODE_EXT:
        return "codigo"
    if ext in DOC_EXT:
        return "documentos"
    return "contexto"


def file_kind(filename: str) -> str:
    """Attachment kind of the database: image, audio or file."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return "image" if ext in IMAGE_EXT else "audio" if ext in AUDIO_EXT else "file"


def clean_filename(name: str) -> str:
    return safe_name(name)


def project_root(path: str) -> Path:
    """The project folder, which must exist and must itself be a real directory (not a link)."""
    p = Path(path)
    try:
        st = os.lstat(p)
    except OSError as exc:
        raise ProjectError("folder_missing", 404) from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ProjectError("folder_unsafe", 400)
    return p
