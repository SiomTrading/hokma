"""Document reading for project libraries (SPEC-009): format readers and the passage splitter."""
from hokma.docs.chunker import CHUNK_TARGET, normalize_text, split_section
from hokma.docs.readers import (
    MAX_PAGES,
    NO_TEXT_NOTICE,
    MAX_PASSAGES,
    READ_SECONDS,
    ReadResult,
    Truncation,
    UnreadableDocument,
    UnsupportedFormat,
    detect_format,
    read_document,
    supported_extensions,
)

__all__ = [
    "CHUNK_TARGET", "MAX_PAGES", "NO_TEXT_NOTICE", "MAX_PASSAGES", "READ_SECONDS", "ReadResult", "Truncation", "UnreadableDocument",
    "UnsupportedFormat", "detect_format", "normalize_text", "read_document", "split_section", "supported_extensions",
]
