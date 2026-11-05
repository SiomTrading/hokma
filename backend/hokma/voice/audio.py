"""Audio type detection from the bytes. The declared type is only a fallback for containers with no magic number."""


def sniff_audio(data: bytes, declared: str = "") -> str | None:
    """MIME type of a supported audio container, or None when the bytes are not audio."""
    if data.startswith(b"\x1a\x45\xdf\xa3"):                       # EBML: what MediaRecorder writes in Chrome/Firefox
        return "audio/webm"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"fLaC"):
        return "audio/flac"
    if data[4:8] == b"ftyp":                                       # MP4 family: Safari MediaRecorder, .m4a files
        return "audio/mp4"
    if data.startswith(b"ID3") or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if data.startswith((b"\xff\xf1", b"\xff\xf9")):
        return "audio/aac"
    if declared.startswith("audio/") and declared in ("audio/amr", "audio/3gpp", "audio/x-ms-wma"):
        return declared
    return None


EXTENSIONS = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/wav": "wav", "audio/flac": "flac",
              "audio/mp4": "m4a", "audio/mpeg": "mp3", "audio/aac": "aac"}
