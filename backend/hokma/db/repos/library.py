"""The student's single Library: its disk folders per project and its own folders (a tree), on SQLite and Postgres."""
from __future__ import annotations

from hokma.db.errors import DuplicateName, NotFound, ValidationError
from hokma.db.models import Document, Library, LibraryFolder, ProjectFolder
from hokma.db.repos.base import Repo, require_in
from hokma.db.timeutil import utcnow

FOLDER_STATUSES = ("scanning", "ready", "missing", "failed")
MAX_NAME = 80
MAX_DEPTH = 8
_BAD_NAME_CHARS = ("/", "\\")


def clean_folder_name(name: str) -> str:
    """The name of a Library folder, trimmed; raises ValidationError when it is empty, too long or holds a slash."""
    name = (name or "").strip()
    if not (1 <= len(name) <= MAX_NAME):
        raise ValidationError(f"folder name must have between 1 and {MAX_NAME} characters")
    if any(ch in name for ch in _BAD_NAME_CHARS):
        raise ValidationError("folder name cannot contain / or \\")
    return name


class LibraryRepo(Repo):
    # ---------------------------------------------------------------------------------------- the library
    def find_student_library(self) -> Library | None:
        row = self.s.one("SELECT * FROM libraries WHERE scope = 'student'")
        return Library.from_row(row) if row else None

    def ensure_student_library(self) -> Library:
        """The one Library of the installation; created on first use, the same row afterwards (also when two callers
        arrive together: the partial unique index makes the second insert a no-op)."""
        existing = self.find_student_library()
        if existing:
            return existing
        self.s.execute(
            "INSERT INTO libraries (scope, project_id, name, created_at) VALUES ('student', NULL, 'Library', :now) "
            "ON CONFLICT (scope) WHERE scope = 'student' DO NOTHING", {"now": utcnow()})
        library = self.find_student_library()
        if library is None:                                                   # pragma: no cover
            raise NotFound("student library could not be created")
        return library

    # ---------------------------------------------------------------------------------------- disk folders
    def add_project_folder(self, project_id: int, path: str, *, label: str | None = None) -> ProjectFolder:
        """Register a disk folder for a project, in status `scanning`. Raises DuplicateName when it is already there."""
        path = (path or "").strip()
        if not path:
            raise ValidationError("path is required")
        if self.s.one("SELECT id FROM projects WHERE id = :p", {"p": project_id}) is None:
            raise NotFound(f"projects {project_id!r} not found")
        if self.s.one("SELECT id FROM project_folders WHERE project_id = :p AND path = :x",
                      {"p": project_id, "x": path}):
            raise DuplicateName("the project already reads from that folder")
        new_id = self.s.insert("project_folders", {
            "project_id": project_id, "path": path, "label": (label or "").strip() or None, "status": "scanning",
            "file_count": 0, "created_at": utcnow()})
        return self.get_project_folder(new_id)

    def get_project_folder(self, id: int) -> ProjectFolder:
        return self._get("project_folders", "id", id, ProjectFolder)

    def list_project_folders(self, project_id: int) -> list[ProjectFolder]:
        rows = self.s.query("SELECT * FROM project_folders WHERE project_id = :p ORDER BY id", {"p": project_id})
        return [ProjectFolder.from_row(r) for r in rows]

    def set_project_folder_status(self, id: int, status: str, detail: str | None = None, *,
                                  file_count: int | None = None, scanned: bool = False) -> ProjectFolder:
        """Change the status of a disk folder; `scanned=True` stamps `last_scan_at`."""
        require_in("status", status, FOLDER_STATUSES)
        self.get_project_folder(id)
        values: dict = {"status": status, "status_detail": detail}
        if file_count is not None:
            if file_count < 0:
                raise ValidationError("file_count cannot be negative")
            values["file_count"] = int(file_count)
        if scanned:
            values["last_scan_at"] = utcnow()
        self.s.update("project_folders", {"id": id}, values)
        return self.get_project_folder(id)

    def remove_project_folder(self, id: int) -> ProjectFolder:
        """Delete a disk folder with the documents it indexed (their passages and vectors follow). The files on the
        disk are never touched."""
        folder = self.get_project_folder(id)
        self.s.execute("DELETE FROM project_folders WHERE id = :i", {"i": id})
        return folder

    # ---------------------------------------------------------------------------------------- Library folders
    def get_folder(self, id: int) -> LibraryFolder:
        return self._get("library_folders", "id", id, LibraryFolder)

    def list_folders(self, library_id: int) -> list[LibraryFolder]:
        rows = self.s.query("SELECT * FROM library_folders WHERE library_id = :l ORDER BY id", {"l": library_id})
        return [LibraryFolder.from_row(r) for r in rows]

    def folder_chain(self, id: int) -> list[LibraryFolder]:
        """The folder and its ancestors, from the top level down (the breadcrumbs)."""
        chain: list[LibraryFolder] = []
        seen: set[int] = set()
        current: int | None = id
        while current is not None:
            if current in seen:                                               # pragma: no cover (cycle guard)
                break
            seen.add(current)
            folder = self.get_folder(current)
            chain.append(folder)
            current = folder.parent_id
        chain.reverse()
        return chain

    def folder_path(self, id: int) -> str:
        """`Papers/2026`: the names from the top level down. Computed, never stored."""
        return "/".join(f.name for f in self.folder_chain(id))

    def descendant_ids(self, id: int) -> list[int]:
        """The folder and every folder below it (what a search filtered by folder includes), ordered."""
        self.get_folder(id)
        rows = self.s.query(
            "WITH RECURSIVE tree(id) AS (SELECT id FROM library_folders WHERE id = :i "
            "UNION ALL SELECT f.id FROM library_folders f JOIN tree t ON f.parent_id = t.id) "
            "SELECT id FROM tree ORDER BY id", {"i": id})
        return [int(r["id"]) for r in rows]

    def create_folder(self, library_id: int, name: str, *, parent_id: int | None = None) -> LibraryFolder:
        """A folder in the Library, at the top level or under `parent_id`. Names are unique among siblings without
        regard to case; the tree is at most MAX_DEPTH levels deep."""
        name = clean_folder_name(name)
        self._require_student_library(library_id)
        depth = 1
        if parent_id is not None:
            parent = self.get_folder(parent_id)
            if parent.library_id != library_id:
                raise ValidationError("the parent folder belongs to another library")
            depth = len(self.folder_chain(parent_id)) + 1
        if depth > MAX_DEPTH:
            raise ValidationError(f"folders can be at most {MAX_DEPTH} levels deep")
        self._require_free_name(library_id, parent_id, name)
        new_id = self.s.insert("library_folders", {
            "library_id": library_id, "parent_id": parent_id, "name": name, "created_at": utcnow()})
        return self.get_folder(new_id)

    def rename_folder(self, id: int, name: str) -> LibraryFolder:
        folder = self.get_folder(id)
        name = clean_folder_name(name)
        self._require_free_name(folder.library_id, folder.parent_id, name, except_id=id)
        self.s.update("library_folders", {"id": id}, {"name": name})
        return self.get_folder(id)

    def folder_contents(self, id: int) -> dict[str, int]:
        """How many documents and subfolders are inside the folder, at any depth (what removing it would delete)."""
        ids = self.descendant_ids(id)
        marks = ", ".join(f":f{i}" for i in range(len(ids)))
        params = {f"f{i}": value for i, value in enumerate(ids)}
        documents = int(self.s.scalar(f"SELECT count(*) FROM documents WHERE library_folder_id IN ({marks})", params))
        return {"documents": documents, "folders": len(ids) - 1}

    def remove_folder(self, id: int) -> list[Document]:
        """Delete the folder, its subfolders and every document inside them (passages and vectors follow). Returns
        the removed documents so the caller deletes their stored copies."""
        ids = self.descendant_ids(id)
        marks = ", ".join(f":f{i}" for i in range(len(ids)))
        params = {f"f{i}": value for i, value in enumerate(ids)}
        rows = self.s.query(f"SELECT * FROM documents WHERE library_folder_id IN ({marks}) ORDER BY id", params)
        self.s.execute("DELETE FROM library_folders WHERE id = :i", {"i": id})
        return [Document.from_row(r) for r in rows]

    def move_document(self, document_id: int, folder_id: int | None) -> Document:
        """Put a Library document in a folder, or back at the top level with `folder_id=None`."""
        row = self.s.one("SELECT * FROM documents WHERE id = :d", {"d": document_id})
        if row is None:
            raise NotFound(f"documents {document_id!r} not found")
        doc = Document.from_row(row)
        library = self._require_student_library(doc.library_id)
        if folder_id is not None and self.get_folder(folder_id).library_id != library.id:
            raise ValidationError("the folder belongs to another library")
        self.s.update("documents", {"id": document_id}, {"library_folder_id": folder_id})
        return Document.from_row(self.s.one("SELECT * FROM documents WHERE id = :d", {"d": document_id}))

    # ---------------------------------------------------------------------------------------- helpers
    def _require_student_library(self, library_id: int) -> Library:
        row = self.s.one("SELECT * FROM libraries WHERE id = :l", {"l": library_id})
        if row is None:
            raise NotFound(f"libraries {library_id!r} not found")
        library = Library.from_row(row)
        if library.scope != "student":
            raise ValidationError("folders exist only in the student library")
        return library

    def _require_free_name(self, library_id: int, parent_id: int | None, name: str, *, except_id: int = 0) -> None:
        if parent_id is None:
            rows = self.s.query("SELECT id, name FROM library_folders WHERE library_id = :l AND parent_id IS NULL",
                                {"l": library_id})
        else:
            rows = self.s.query("SELECT id, name FROM library_folders WHERE library_id = :l AND parent_id = :p",
                                {"l": library_id, "p": parent_id})
        wanted = name.casefold()
        if any(r["id"] != except_id and r["name"].casefold() == wanted for r in rows):
            raise DuplicateName("a folder with that name already exists here")
