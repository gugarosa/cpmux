# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

from pathlib import Path

DEFAULT_TRANSCRIBE_MODEL = "base"


class VoiceError(Exception):
    """Recording, transcription, or synthesis failed."""


def transcribe(audio_path: str | Path, model: str = DEFAULT_TRANSCRIBE_MODEL) -> str:
    """Transcribe audio on-device with faster-whisper.

    Args:
        audio_path: Audio file to transcribe.
        model: Whisper model size (``tiny`` … ``large-v3``) or a Hugging Face model id.

    Returns:
        Transcribed text.

    Raises:
        VoiceError: If faster-whisper is missing or transcription returns no text.

    """

    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise VoiceError(f"`{audio_path}` audio file does not exist.")

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise VoiceError("transcription needs faster-whisper, install `cmux[voice]`.") from exc

    try:
        whisper = WhisperModel(model, device="cpu", compute_type="int8")
        segments, _ = whisper.transcribe(str(audio_path))
        text = " ".join(segment.text for segment in segments).strip()
    except Exception as exc:
        raise VoiceError(f"`{model}` transcription failed: {exc}.") from exc

    if not text:
        raise VoiceError("transcription returned no text.")

    return text
