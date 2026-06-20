"""WeChat SILK voice -> text, fully local / offline (no cloud, no ffmpeg).

WeChat voice messages are Tencent **SILK v3**: the on-disk/blob payload starts
with a single ``0x02`` framing byte followed by the ``b"#!SILK_V3"`` magic. We
strip that leading byte, decode SILK to raw PCM (s16le, mono) at 24000 Hz with
``silk-python`` (it installs under the import name ``pysilk`` — NOT ``pilk``,
which has no macOS wheel), resample to 16000 Hz, and
feed Whisper a numpy ``float32`` mono array directly. Passing the array (rather
than a file path) is what lets us avoid spawning ffmpeg.

Transcription runs entirely on the local machine — nothing is uploaded. Results
are cached on disk keyed by the md5 of the SILK bytes so re-exports are cheap.

Engines:
* ``faster`` (default): ``faster-whisper`` — int8 / CPU, no PyTorch.
* ``openai``: the reference ``openai-whisper`` (heavier; pulls PyTorch).
"""
from __future__ import annotations

import hashlib
import io
import os
from typing import Callable, List, Optional

from . import constants
from .models import Message

#: SILK constants. Payload = 0x02 framing byte + this magic.
_SILK_FRAME = b"\x02"
_SILK_MAGIC = b"#!SILK_V3"

#: Sample rates: SILK decodes to 24000 Hz, Whisper expects 16000 Hz.
_DECODE_RATE = 24000
_TARGET_RATE = 16000


class VoiceConverter:
    """Lazy, cached SILK-to-text transcriber backed by a local Whisper model."""

    def __init__(
        self,
        engine: str = "faster",
        model: str = "base",
        cache_dir: Optional[str] = None,
        language: str = "zh",
    ) -> None:
        self.engine = engine
        self.model_name = model
        self.language = language
        self.cache_dir = os.path.expanduser(
            cache_dir if cache_dir is not None else constants.VOICE_CACHE_DIR
        )
        os.makedirs(self.cache_dir, exist_ok=True)
        # Loaded on first transcription (see _ensure_model).
        self._model = None  # type: ignore[assignment]

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def transcribe(self, silk: "str|bytes") -> str:
        """Transcribe one SILK voice (path or raw DB blob) to text.

        Returns "" for empty input. Caches by md5(silk bytes). On a decode or
        STT failure the error is swallowed and "" is returned so a single bad
        clip never aborts a whole export.
        """
        data = self._read_bytes(silk)
        if not data:
            return ""

        key = hashlib.md5(data).hexdigest()
        cache_path = os.path.join(self.cache_dir, key + ".txt")
        cached = self._read_cache(cache_path)
        if cached is not None:
            return cached

        try:
            audio = self._silk_to_audio(data)
            text = self._run_stt(audio)
        except _MissingDependency:
            # A required package is genuinely absent — surface it loudly so the
            # user can `pip install` it. Never silently produce empty text.
            raise
        except Exception:
            # Corrupt / truncated / non-SILK clip: degrade to empty, keep going.
            text = ""

        self._write_cache(cache_path, text)
        return text

    def close(self) -> None:
        """Release the loaded model (frees the Whisper weights from memory)."""
        self._model = None

    # ------------------------------------------------------------------ #
    # Input handling
    # ------------------------------------------------------------------ #
    @staticmethod
    def _read_bytes(silk: "str|bytes") -> bytes:
        """Accept a filesystem path (str) or raw bytes; return the bytes."""
        if silk is None:
            return b""
        if isinstance(silk, (bytes, bytearray)):
            return bytes(silk)
        # Treat str as a path.
        path = os.path.expanduser(silk)
        if not os.path.isfile(path):
            return b""
        with open(path, "rb") as fh:
            return fh.read()

    # ------------------------------------------------------------------ #
    # SILK -> numpy float32 mono @ 16000 Hz
    # ------------------------------------------------------------------ #
    def _silk_to_audio(self, data: bytes):
        """Decode SILK bytes to a float32 mono numpy array at 16000 Hz."""
        np = _import_numpy()
        silk = _import_silk()

        # Strip the single leading 0x02 framing byte, then require the magic.
        if data[:1] == _SILK_FRAME:
            data = data[1:]
        if not data.startswith(_SILK_MAGIC):
            raise ValueError("不是有效的 SILK_V3 语音数据 (缺少 #!SILK_V3 标记)")

        # silk-python decodes file-like -> file-like at a given sample rate.
        # Signature is silk.decode(in_io, out_io, sample_rate); we pass the
        # decode rate (24000). Older builds expose the same positional form, so
        # this is the documented, portable call.
        in_io = io.BytesIO(data)
        out_io = io.BytesIO()
        silk.decode(in_io, out_io, _DECODE_RATE)
        pcm = out_io.getvalue()
        if not pcm:
            raise ValueError("SILK 解码结果为空")

        # PCM s16le -> float32 in [-1, 1].
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

        if _DECODE_RATE != _TARGET_RATE:
            samples = _resample(samples, _DECODE_RATE, _TARGET_RATE, np)
        return samples

    # ------------------------------------------------------------------ #
    # Speech-to-text
    # ------------------------------------------------------------------ #
    def _ensure_model(self):
        """Lazy-load the Whisper model for the configured engine."""
        if self._model is not None:
            return self._model

        if self.engine == "faster":
            try:
                from faster_whisper import WhisperModel  # type: ignore
            except ImportError as exc:
                raise _MissingDependency(
                    "未安装 faster-whisper，语音转文字无法使用。"
                    "请运行: pip install faster-whisper"
                ) from exc
            self._model = WhisperModel(
                self.model_name, device="cpu", compute_type="int8"
            )
        elif self.engine == "openai":
            try:
                import whisper  # type: ignore
            except ImportError as exc:
                raise _MissingDependency(
                    "未安装 openai-whisper，语音转文字无法使用。"
                    "请运行: pip install openai-whisper (并确保系统已安装 ffmpeg)"
                ) from exc
            self._model = whisper.load_model(self.model_name)
        else:
            raise ValueError(
                "未知的语音引擎: %r (可选: 'faster' 或 'openai')" % (self.engine,)
            )
        return self._model

    def _run_stt(self, audio) -> str:
        """Transcribe a float32 mono @16k numpy array to stripped text."""
        model = self._ensure_model()
        if self.engine == "faster":
            segments, _info = model.transcribe(
                audio, language=self.language, beam_size=5
            )
            return "".join(seg.text for seg in segments).strip()
        # openai
        result = model.transcribe(audio, language=self.language)
        return str(result.get("text", "")).strip()

    # ------------------------------------------------------------------ #
    # Cache helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _read_cache(path: str) -> Optional[str]:
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None

    @staticmethod
    def _write_cache(path: str, text: str) -> None:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            # Cache is best-effort; never let a write failure break a transcribe.
            pass


# --------------------------------------------------------------------------- #
# Batch driver
# --------------------------------------------------------------------------- #
def transcribe_messages(
    messages: List[Message],
    converter: VoiceConverter,
    progress: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Fill ``voice_text`` for every voice message in ``messages``.

    For each message with ``needs_voice`` set, transcribe its source (the
    ``media_src`` path, or a raw blob stashed under ``extra["voice_blob"]``) and
    store the result on ``msg.voice_text``. ``progress`` is an optional callable
    invoked as ``progress(done, total)`` after each conversion. Returns the
    number of messages actually converted (non-empty transcription).
    """
    voice_msgs = [m for m in messages if getattr(m, "needs_voice", False)]
    total = len(voice_msgs)
    converted = 0

    for idx, msg in enumerate(voice_msgs, start=1):
        # Prefer the raw SILK blob (correct input for the decoder); media_src may
        # have been repointed to a playback WAV by the media resolver.
        src = msg.extra.get("voice_blob") or msg.media_src
        if src:
            text = converter.transcribe(src)
            msg.voice_text = text
            if text:
                converted += 1
        if progress is not None:
            progress(idx, total)

    return converted


# --------------------------------------------------------------------------- #
# Internal: lazy imports & resampling
# --------------------------------------------------------------------------- #
class _MissingDependency(ImportError):
    """Raised when a required optional package is absent (carries a zh message)."""


def _import_numpy():
    try:
        import numpy as np  # type: ignore
    except ImportError as exc:
        raise _MissingDependency(
            "未安装 numpy，语音转文字无法使用。请运行: pip install numpy"
        ) from exc
    return np


def _import_silk():
    # silk-python (>=0.2.8) installs under the import name ``pysilk``; some
    # older/alternate builds expose ``silk``. Try both; NOT ``pilk``.
    for name in ("pysilk", "silk"):
        try:
            return __import__(name)
        except ImportError:
            continue
    raise _MissingDependency(
        "未安装 silk-python，无法解码 SILK 语音。请运行: pip install silk-python"
    )


def _resample(samples, src_rate: int, dst_rate: int, np):
    """Resample a 1-D float32 array. Prefer soxr; fall back to scipy/numpy."""
    # Best quality: soxr.
    try:
        import soxr  # type: ignore

        return soxr.resample(samples, src_rate, dst_rate).astype(np.float32)
    except ImportError:
        pass

    # Decent fallback: scipy polyphase resampling.
    try:
        from math import gcd

        from scipy.signal import resample_poly  # type: ignore

        g = gcd(src_rate, dst_rate)
        up = dst_rate // g
        down = src_rate // g
        return resample_poly(samples, up, down).astype(np.float32)
    except ImportError:
        pass

    # Last resort: linear interpolation with pure numpy (lower quality).
    n_src = samples.shape[0]
    if n_src == 0:
        return samples
    n_dst = int(round(n_src * dst_rate / float(src_rate)))
    if n_dst <= 0:
        return samples[:0]
    src_idx = np.arange(n_src, dtype=np.float64)
    dst_idx = np.linspace(0.0, n_src - 1, n_dst)
    return np.interp(dst_idx, src_idx, samples).astype(np.float32)
