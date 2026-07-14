# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import sys
import threading
import time
import wave
from array import array
from pathlib import Path

from rich.text import Text

from cpmux import theme
from cpmux.voice.transcriber import VoiceError

_SAMPLE_RATE = 16000
_CHANNELS = 1
_SAMPLE_WIDTH = 2
_METER_WIDTH = 24
_METER_GAIN = 2.5


def record_to_file(path: str | Path) -> Path:
    """Record the default microphone until Enter is pressed.

    Args:
        path: Destination audio file.

    Returns:
        The destination path.

    Raises:
        VoiceError: If capture is unavailable or fails.

    """

    try:
        import sounddevice
    except ImportError as exc:
        raise VoiceError("`sounddevice` is unavailable; install `cpmux[voice]`.") from exc

    frames: list[bytes] = []
    level = _Level()

    def on_audio(indata: object, _frames: int, _time: object, _status: object) -> None:
        """Collect an audio block and update the input level."""

        block = bytes(indata)
        frames.append(block)
        level.update(block)

    try:
        stream = sounddevice.RawInputStream(
            samplerate=_SAMPLE_RATE, channels=_CHANNELS, dtype="int16", callback=on_audio
        )
        with stream:
            _wait_until_stopped(level)
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


class _Level:
    def __init__(self) -> None:
        self.value = 0.0

    def update(self, block: bytes) -> None:
        """Update the peak level from an audio block."""

        samples = array("h")
        samples.frombytes(block[: len(block) - len(block) % _SAMPLE_WIDTH])
        if not samples:
            return

        peak = max(abs(samples[index]) for index in range(0, len(samples), 8)) / 32768.0
        self.value = max(peak, self.value * 0.8)


def _wait_until_stopped(level: _Level) -> None:
    if not sys.stdout.isatty():
        input("🎙  Recording — press Enter to stop")
        return

    console = theme.err
    stopped = threading.Event()
    threading.Thread(target=_wait_for_enter, args=(stopped,), daemon=True).start()

    start = time.monotonic()
    with console.status("", spinner="dots") as status:
        while not stopped.is_set():
            status.update(_meter(time.monotonic() - start, level.value))
            time.sleep(0.06)

    console.print(f"[green]✓[/green] captured [bold]{_elapsed(time.monotonic() - start)}[/bold]")


def _wait_for_enter(stopped: threading.Event) -> None:
    try:
        input()
    except EOFError:
        pass

    stopped.set()


def _meter(elapsed: float, level: float) -> Text:
    filled = round(min(1.0, level * _METER_GAIN) * _METER_WIDTH)

    line = Text()
    line.append("🎙  Recording  ", style="bold red")
    line.append(_elapsed(elapsed), style="bold white")
    line.append("  ▕")
    line.append("█" * filled, style=_level_style(level))
    line.append("░" * (_METER_WIDTH - filled), style="grey37")
    line.append("▏  ")
    line.append("Enter to stop", style="dim")

    return line


def _level_style(level: float) -> str:
    if level > 0.66:
        return "bold red"
    if level > 0.33:
        return "yellow"
    return "green"


def _elapsed(seconds: float) -> str:
    total = int(seconds)

    return f"{total // 60}:{total % 60:02d}"
