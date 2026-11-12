"""Query-time access to the embedding model for processes that do not own the download (SPEC-010 P13).

The agent tools run in their own process (the Hokma tool server), where the downloader of the main process is not
available. Readiness is therefore read from the disk: the model is ready when every file of the manifest is in place
with its expected size. The downloader only renames a file into place after its sha256 matched, so a file at its final
name with the right size has been verified already.
"""
import threading
from pathlib import Path

from hokma.library.model_download import load_manifest, models_dir

_lock = threading.Lock()
_embedder = None
_embedder_key: Path | None = None


def model_id() -> str:
    """Identifier stored with every vector (the manifest id)."""
    return load_manifest()["id"]


def model_directory() -> Path:
    return models_dir() / load_manifest()["id"]


def model_ready() -> bool:
    """True when every model file is in place at its final name with the expected size."""
    directory = model_directory()
    for entry in load_manifest()["files"]:
        try:
            if (directory / entry["name"]).stat().st_size != entry["size"]:
                return False
        except OSError:
            return False
    return True


def _session():
    """The model session, built on first use and kept for the life of the process."""
    global _embedder, _embedder_key
    from hokma.library.embedder import Embedder

    directory = model_directory()
    with _lock:
        if _embedder is None or _embedder_key != directory:
            if _embedder is not None:
                _embedder.close()
            manifest = load_manifest()
            _embedder = Embedder(directory / manifest["files"][0]["name"], directory / manifest["files"][1]["name"])
            _embedder_key = directory
        return _embedder


def embed_query(text: str) -> list[float]:
    """The vector of one question. Raises EmbedderError when the model cannot run."""
    return _session().embed_query(text).tolist()


def embed_passages(texts: list[str]) -> list[list[float]]:
    """One vector per passage text (the model's passage prefix is added here). Raises EmbedderError."""
    return [v.tolist() for v in _session().embed_passages(texts)]


def reset_for_tests() -> None:
    global _embedder, _embedder_key
    with _lock:
        if _embedder is not None:
            _embedder.close()
        _embedder, _embedder_key = None, None
