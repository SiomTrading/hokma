"""One error type for the projects API. `code` is stable; the message is localized from the catalog."""
from typing import Callable

from hokma.i18n import t

# One literal t(...) per message, so the catalog check can see that every key is used.
_MESSAGES: dict[str, Callable[..., str]] = {
    "bad_token": lambda **p: t("projects.err.bad_token", **p),
    "bad_path": lambda **p: t("projects.err.bad_path", **p),
    "folder_missing": lambda **p: t("projects.err.folder_missing", **p),
    "folder_unsafe": lambda **p: t("projects.err.folder_unsafe", **p),
    "not_found": lambda **p: t("projects.err.not_found", **p),
    "bad_name": lambda **p: t("projects.err.bad_name", **p),
    "bad_kind": lambda **p: t("projects.err.bad_kind", **p),
    "folder_create": lambda **p: t("projects.err.folder_create", **p),
    "db_failed": lambda **p: t("projects.err.db_failed", **p),
    "empty_file": lambda **p: t("projects.err.empty_file", **p),
    "too_large": lambda **p: t("projects.err.too_large", **p),
    "no_file": lambda **p: t("projects.err.no_file", **p),
    "file_not_found": lambda **p: t("projects.err.file_not_found", **p),
    "nothing_to_save": lambda **p: t("projects.err.nothing_to_save", **p),
    "too_many": lambda **p: t("projects.err.too_many", **p),
    "attachment_not_found": lambda **p: t("projects.err.attachment_not_found", **p),
    "bad_note": lambda **p: t("projects.err.bad_note", **p),
    "bad_url": lambda **p: t("projects.err.bad_url", **p),
    "url_not_https": lambda **p: t("projects.err.url_not_https", **p),
    "url_port": lambda **p: t("projects.err.url_port", **p),
    "dns_failed": lambda **p: t("projects.err.dns_failed", **p),
    "url_private": lambda **p: t("projects.err.url_private", **p),
    "http_status": lambda **p: t("projects.err.http_status", **p),
    "empty_download": lambda **p: t("projects.err.empty_download", **p),
    "type_blocked": lambda **p: t("projects.err.type_blocked", **p),
    "too_many_redirects": lambda **p: t("projects.err.too_many_redirects", **p),
    "connect_failed": lambda **p: t("projects.err.connect_failed", **p),
    "timeout": lambda **p: t("projects.err.timeout", **p),
    "doc_duplicate": lambda **p: t("docs.err.duplicate", **p),
    "doc_too_large": lambda **p: t("docs.err.too_large", **p),
    "doc_unsupported": lambda **p: t("docs.err.unsupported", **p),
    "doc_not_found": lambda **p: t("docs.err.not_found", **p),
    "doc_name_taken": lambda **p: t("docs.err.name_taken", **p),
    "lib_folder_not_found": lambda **p: t("docs.err.folder_not_found", **p),
    "duplicate_name": lambda **p: t("docs.err.folder_taken", **p),
    "folder_not_empty": lambda **p: t("docs.err.folder_not_empty", **p),
    "folder_bad_name": lambda **p: t("docs.err.folder_bad_name", **p),
    "folder_too_deep": lambda **p: t("docs.err.folder_too_deep", **p),
    "owner_confirmation_required": lambda **p: t("docs.disk.err.owner_required", **p),
    "disk_path_missing": lambda **p: t("docs.disk.err.path_missing", **p),
    "disk_not_folder": lambda **p: t("docs.disk.err.not_folder", **p),
    "disk_root": lambda **p: t("docs.disk.err.root", **p),
    "disk_home": lambda **p: t("docs.disk.err.home", **p),
    "disk_data_dir": lambda **p: t("docs.disk.err.data_dir", **p),
    "disk_system": lambda **p: t("docs.disk.err.system", **p),
    "disk_duplicate": lambda **p: t("docs.disk.err.duplicate", **p),
    "disk_too_many": lambda **p: t("docs.disk.err.too_many", **p),
    "disk_own_folder": lambda **p: t("docs.disk.err.own_folder", **p),
    "disk_folder_not_found": lambda **p: t("docs.disk.err.not_found", **p),
    "link_blocked": lambda **p: t("docs.link.blocked", **p),
    "link_not_document": lambda **p: t("docs.link.not_document", **p),
    "link_scheme": lambda **p: t("docs.link.scheme", **p),
    "model_not_failed": lambda **p: t("library.err.model_not_failed", **p),
}


class ProjectError(Exception):
    def __init__(self, code: str, status: int = 400, *, extra: dict[str, object] | None = None,
                 **params: object) -> None:
        self.code = code
        self.status = status
        self.params = params
        self.extra = extra                      # machine-readable fields added next to code and message
        super().__init__(_MESSAGES[code](**params))
