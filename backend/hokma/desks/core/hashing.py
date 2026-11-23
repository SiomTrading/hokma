"""Content hash and stable rendering that ignore generation dates (SPEC-017: AH-47, AH-48).

The implementation is in `hokma.core.contenthash` (importable by worker processes); desks use it from here.
"""
from hokma.core.contenthash import VOLATILE_KEYS, content_hash, render

__all__ = ["VOLATILE_KEYS", "content_hash", "render"]
