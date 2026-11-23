"""Multi model layer (SPEC-017, SVC-llm): one way for the desks to ask a model, whichever engine the student connected.

It sits on `hokma.engines` (Claude Code, Codex, Gemini CLI, direct API) and creates no HTTP client of its own: the
engines already hold the connections, the keys and the permission rules.
"""
from hokma.llm.factory import make_llm
from hokma.llm.jsonx import LLMParseError, extract_json
from hokma.llm.protocol import ChatLLM, LLMClient, LLMError, LLMReply

__all__ = ["ChatLLM", "LLMClient", "LLMError", "LLMParseError", "LLMReply", "extract_json", "make_llm"]
