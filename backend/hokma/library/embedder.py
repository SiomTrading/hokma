"""Local text embedder for the student library (SPEC-010 §4.4).

Runs the quantized multilingual-e5-small ONNX model with `onnxruntime` and `tokenizers` (no torch).
Questions get the `query: ` prefix and passages the `passage: ` prefix, the output is the mean over the real
tokens (padding excluded), then L2-normalized, so a dot product is a cosine similarity.
All inference happens in one dedicated worker thread with lowered priority and at most 2 compute threads, so
the student's own work is never slowed down."""
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Sequence

import numpy as np

QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "
DIMENSIONS = 384
MAX_TOKENS = 512
BATCH_SIZE = 16
MAX_THREADS = 2
LOW_PRIORITY_NICE = 10


class EmbedderError(Exception):
    """The model could not be loaded or run. Never shown raw to the student."""


def lower_current_thread_priority() -> None:
    """Best effort: lower the priority of the calling thread. Threads created later inherit it on Linux."""
    try:
        if sys.platform == "win32":
            import ctypes
            below_normal = -1  # THREAD_PRIORITY_BELOW_NORMAL
            kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel.SetThreadPriority(kernel.GetCurrentThread(), below_normal)
        elif sys.platform.startswith("linux"):
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), LOW_PRIORITY_NICE)
        # macOS: no per-thread priority from Python; the thread count cap is the only limit there.
    except (OSError, AttributeError, ValueError):
        pass


class Embedder:
    def __init__(self, model_path: Path, tokenizer_path: Path, threads: int = MAX_THREADS,
                 batch_size: int = BATCH_SIZE, max_tokens: int = MAX_TOKENS) -> None:
        self.model_path = Path(model_path)
        self.tokenizer_path = Path(tokenizer_path)
        self.threads = max(1, min(int(threads), MAX_THREADS))
        self.batch_size = max(1, int(batch_size))
        self.max_tokens = max(2, min(int(max_tokens), MAX_TOKENS))
        self._tokenizer = None
        self._session = None
        self._input_names: set[str] = set()
        # One worker: inference is serialized and always runs at low priority.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hokma-embed",
                                        initializer=lower_current_thread_priority)

    # --- public ------------------------------------------------------------------------------------------------
    def embed_query(self, text: str) -> np.ndarray:
        """One question -> a float32 vector of 384 dimensions, L2-normalized."""
        return self._run([QUERY_PREFIX + text])[0]

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        """Passages -> float32 matrix (len(texts), 384), L2-normalized, in the same order as the input."""
        if not texts:
            return np.zeros((0, DIMENSIONS), dtype=np.float32)
        return self._run([PASSAGE_PREFIX + text for text in texts])

    def close(self) -> None:
        self._pool.shutdown(wait=True)

    # --- internals ---------------------------------------------------------------------------------------------
    def _run(self, prefixed: list[str]) -> np.ndarray:
        try:
            return self._pool.submit(self._embed_all, prefixed).result()
        except EmbedderError:
            raise
        except Exception as exc:  # onnxruntime / tokenizers errors
            raise EmbedderError(str(exc)) from exc

    def _load(self) -> None:
        if self._session is not None:
            return
        import onnxruntime as ort
        from tokenizers import Tokenizer

        tokenizer = Tokenizer.from_file(str(self.tokenizer_path))
        pad_id = tokenizer.token_to_id("<pad>")
        tokenizer.enable_truncation(max_length=self.max_tokens)
        tokenizer.enable_padding(pad_id=pad_id if pad_id is not None else 1, pad_token="<pad>")
        options = ort.SessionOptions()
        options.intra_op_num_threads = self.threads
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(str(self.model_path), sess_options=options, providers=["CPUExecutionProvider"])
        self._tokenizer = tokenizer
        self._input_names = {item.name for item in session.get_inputs()}
        self._session = session

    def _embed_all(self, prefixed: list[str]) -> np.ndarray:
        self._load()
        # Similar lengths share a batch so padding is small; the original order is restored at the end.
        order = sorted(range(len(prefixed)), key=lambda i: len(prefixed[i]))
        out = np.zeros((len(prefixed), DIMENSIONS), dtype=np.float32)
        for start in range(0, len(order), self.batch_size):
            ids = order[start:start + self.batch_size]
            out[ids] = self._embed_batch([prefixed[i] for i in ids])
        return out

    def _embed_batch(self, texts: list[str]) -> np.ndarray:
        encodings = self._tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        feeds = {"input_ids": input_ids, "attention_mask": mask}
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.zeros_like(input_ids)
        hidden = self._session.run(None, feeds)[0]  # (batch, tokens, 384)
        weights = mask[:, :, None].astype(np.float32)
        pooled = (hidden * weights).sum(axis=1) / np.maximum(weights.sum(axis=1), 1.0)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        return (pooled / np.maximum(norms, 1e-12)).astype(np.float32)
