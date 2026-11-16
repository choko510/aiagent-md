"""Model-bound tokenizer estimation for context window admission (AI-02 §18.23.3)."""

from __future__ import annotations

import math
import re
from typing import Any, Protocol

_CHUNK_RE = re.compile(r"[A-Za-z0-9_]+|[^\s\w]|[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]")


class TokenEstimatorPort(Protocol):
    @property
    def mode(self) -> str: ...

    def count_tokens(self, text: str, *, model_id: str, tokenizer_id: str) -> int: ...


class ModelBoundTokenEstimator:
    """Uses `tiktoken` when installed and matching the tokenizer/model; otherwise a conservative subword counter."""

    def __init__(self, *, strict_tiktoken: bool = False) -> None:
        self._strict_tiktoken = strict_tiktoken
        self._tiktoken = self._load_tiktoken()

    @staticmethod
    def _load_tiktoken() -> Any:
        try:
            import tiktoken  # type: ignore[import-not-found]
        except ImportError:
            return None
        return tiktoken

    @property
    def mode(self) -> str:
        return "TIKTOKEN" if self._tiktoken is not None else "CONSERVATIVE_APPROX"

    def count_tokens(self, text: str, *, model_id: str, tokenizer_id: str) -> int:
        if not text:
            return 0
        if self._tiktoken is not None:
            tk = self._tiktoken
            enc = None
            for name in (tokenizer_id, "cl100k_base", "o200k_base"):
                try:
                    enc = tk.get_encoding(name)  # type: ignore[attr-defined]
                    break
                except Exception:
                    enc = None
            if enc is None:
                try:
                    enc = tk.encoding_for_model(model_id)  # type: ignore[attr-defined]
                except Exception:
                    enc = None
            if enc is not None:
                return len(enc.encode(text, disallowed_special=())) + 1
        elif self._strict_tiktoken:
            raise RuntimeError("tiktoken unavailable for strict tokenization")
        # Conservative BPE-like approximation: CJK characters map >=1 token each,
        # ASCII runs map ceil(len / 3), punctuation 1 token each, plus floor of ceil(utf8_bytes / 4).
        pieces = 0
        for tok in _CHUNK_RE.findall(text):
            if ord(tok[0]) >= 0x3000:
                pieces += len(tok)
            elif len(tok) <= 3:
                pieces += 1
            else:
                pieces += math.ceil(len(tok) / 3)
        byte_floor = math.ceil(len(text.encode("utf-8")) / 4)
        return max(pieces, byte_floor, 1)
