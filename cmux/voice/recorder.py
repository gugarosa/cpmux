# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import wave
from pathlib import Path

from cmux.voice.transcriber import VoiceError

_SAMPLE_RATE = 16000
_CHANNELS = 1
_SAMPLE_WIDTH = 2


def record_to_file(path: str | Path, prompt: str = "Recording — press Enter to stop") -> Path:
    """Record the default microphone until Enter is pressed.

    Args:
        path: Destination WAV file.
        prompt: Message shown while recording.

    Returns:
        Written WAV file.

    Raises:
        VoiceError: If the audio backend is unavailable or capture fails.

    """

    try:
        import sounddevice
    except ImportError as exc:
        raise VoiceError("microphone capture needs `sounddevice`, install `cmux[voice]`.") from exc

    frames: list[bytes] = []

    def on_audio(indata: object, _frames: int, _time: object, _status: object) -> None:
        frames.append(bytes(indata))

    try:
        stream = sounddevice.RawInputStream(
            samplerate=_SAMPLE_RATE, channels=_CHANNELS, dtype="int16", callback=on_audio
        )
        with stream:
            input(f"🎙  {prompt}")
    except Exception as exc:
        raise VoiceError(f"microphone capture failed: {exc}.") from exc

    if not frames:
        raise VoiceError("no audio was captured.")

    path = Path(path)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(_CHANNELS)
        wav.setsampwidth(_SAMPLE_WIDTH)
        wav.setframerate(_SAMPLE_RATE)
        wav.writeframes(b"".join(frames))

    return path
