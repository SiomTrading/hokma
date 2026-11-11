"""Speech to text. `Transcriber` is what the API needs; `FasterWhisperTranscriber` is the real one.

faster-whisper is imported only when it is needed, so Hokma starts and the rest of the app works without it.
"""
import importlib.util
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from hokma.voice.model import APPROX_DOWNLOAD_MB, ModelManager

LANGUAGE_CHOICES = ("auto", "es_en", "es", "en")
DEFAULT_LANGUAGE = "es_en"             # auto detection limited to the two languages of the owner
SAMPLE_RATE = 16000


@dataclass
class Transcript:
    text: str
    language: str
    duration: float                    # seconds of audio


class Transcriber(Protocol):
    def available(self) -> bool: ...
    def status(self) -> dict[str, Any]: ...
    def start_download(self) -> None: ...
    def transcribe(self, path: Path, language: str) -> Transcript: ...


class FasterWhisperTranscriber:
    def __init__(self, manager: ModelManager | None = None) -> None:
        self.manager = manager or ModelManager()
        self._model = None
        self._load_lock = threading.Lock()
        self._run_lock = threading.Lock()          # one transcription at a time: CPU bound, memory heavy

    def available(self) -> bool:
        # onnxruntime is what faster-whisper runs its voice detector on; it has no build for every platform
        # (task 3173), so both must be there. Without them Hokma starts and only the voice says it is unavailable.
        return all(importlib.util.find_spec(m) is not None for m in ("faster_whisper", "onnxruntime"))

    def status(self) -> dict[str, Any]:
        out = self.manager.snapshot()
        out.update(available=self.available(), model=self.manager.size,
                   download_mb=APPROX_DOWNLOAD_MB.get(self.manager.size, 0))
        return out

    def start_download(self) -> None:
        self.manager.start_download()

    def _load(self):
        with self._load_lock:
            if self._model is None:
                from faster_whisper import WhisperModel
                self._model = WhisperModel(str(self.manager.path), device="cpu", compute_type="int8")
            return self._model

    def transcribe(self, path: Path, language: str) -> Transcript:
        from faster_whisper import decode_audio

        model = self._load()
        with self._run_lock:
            audio = decode_audio(str(path), sampling_rate=SAMPLE_RATE)
            duration = len(audio) / SAMPLE_RATE
            if duration < 0.3:
                return Transcript("", "", duration)
            chosen = self._choose_language(model, audio, language)
            segments, info = model.transcribe(audio, language=chosen, vad_filter=True, beam_size=5)
            text = " ".join(s.text.strip() for s in segments).strip()
            return Transcript(text, info.language or chosen or "", duration)

    @staticmethod
    def _choose_language(model, audio, language: str) -> str | None:
        if language in ("es", "en"):
            return language
        if language == "auto":
            return None
        _, _, probabilities = model.detect_language(audio)
        scores = dict(probabilities)
        return "es" if scores.get("es", 0.0) >= scores.get("en", 0.0) else "en"
