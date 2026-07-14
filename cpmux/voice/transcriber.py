# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

DEFAULT_TRANSCRIBE_MODEL = "large-v3-turbo"


class VoiceError(Exception):
    """Recording, transcription, or synthesis failed."""


def load_model(model: str) -> "WhisperModel":
    """Load a faster-whisper model for on-device CPU inference.

    Args:
        model: Faster-whisper model name.

    Returns:
        The loaded model.

    Raises:
        VoiceError: If faster-whisper is unavailable or the model cannot be loaded.

    """

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise VoiceError("`faster-whisper` is unavailable; install `cpmux[voice]`.") from exc

    try:
        return WhisperModel(model, device="cpu", compute_type="int8")
    except Exception as exc:
        raise VoiceError(f"`{model}` model could not be loaded: {exc}.") from exc


def transcribe_audio(whisper: "WhisperModel", audio: object, language: str | None = None) -> str:
    """Transcribe a file path or float32 sample array with a loaded model.

    Args:
        whisper: Loaded faster-whisper model.
        audio: Audio file path or float32 mono samples.
        language: Spoken language code, or None to auto-detect.

    Returns:
        The transcribed text.

    """

    segments, _ = whisper.transcribe(
        audio,
        language=language,
        vad_filter=True,
        condition_on_previous_text=False,
    )

    return " ".join(segment.text for segment in segments).strip()


def transcribe(audio_path: str | Path, model: str = DEFAULT_TRANSCRIBE_MODEL, language: str | None = None) -> str:
    """Transcribe an audio file on-device with faster-whisper.

    Args:
        audio_path: Audio file to transcribe.
        model: Faster-whisper model name.
        language: Spoken language code, or None to auto-detect.

    Returns:
        The transcribed text.

    Raises:
        VoiceError: If transcription is unavailable or fails.

    """

    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise VoiceError(f"`{audio_path}` audio file does not exist.")

    whisper = load_model(model)
    try:
        text = transcribe_audio(whisper, str(audio_path), language)
    except Exception as exc:
        raise VoiceError(f"`{model}` transcription failed: {exc}.") from exc

    if not text:
        raise VoiceError("transcription returned no text.")

    return text
