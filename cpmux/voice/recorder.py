# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import sys
import threading
import time
from array import array

from rich.console import Group
from rich.live import Live
from rich.text import Text

from cpmux import theme
from cpmux.voice.transcriber import VoiceError, load_model, transcribe_audio

_SAMPLE_RATE = 16000
_CHANNELS = 1
_SAMPLE_WIDTH = 2
_METER_WIDTH = 24
_METER_GAIN = 2.5
_PARTIAL_MODEL = "base"
_PARTIAL_INTERVAL = 1.2
_MIN_PARTIAL_SECONDS = 1.0


def record_and_transcribe(model: str, language: str | None = None) -> str:
    """Record the microphone and transcribe it live until Enter is pressed.

    A fast model streams a partial transcript while recording; the requested
    model produces the accurate final transcript once recording stops.

    Args:
        model: Faster-whisper model for the final transcription.
        language: Spoken language code, or None to auto-detect.

    Returns:
        The final transcript.

    Raises:
        VoiceError: If capture or transcription is unavailable or fails.

    """

    try:
        import numpy
    except ImportError as exc:
        raise VoiceError("`numpy` is unavailable; install `cpmux[voice]`.") from exc
    try:
        import sounddevice
    except ImportError as exc:
        raise VoiceError("`sounddevice` is unavailable; install `cpmux[voice]`.") from exc

    theme.print_hint("loading transcription model…")
    final_model = load_model(model)
    partial_model = final_model if model == _PARTIAL_MODEL else load_model(_PARTIAL_MODEL)

    frames: list[bytes] = []
    level = _Level()
    partial = _Partial()
    stopped = threading.Event()

    def on_audio(indata: object, _frames: int, _time: object, _status: object) -> None:
        block = bytes(indata)
        frames.append(block)
        level.update(block)

    worker: threading.Thread | None = None
    try:
        with sounddevice.RawInputStream(samplerate=_SAMPLE_RATE, channels=_CHANNELS, dtype="int16", callback=on_audio):
            worker = threading.Thread(
                target=_stream_partials, args=(partial_model, frames, language, partial, stopped), daemon=True
            )
            worker.start()
            _wait_until_stopped(level, partial)
    except VoiceError:
        raise
    except Exception as exc:
        raise VoiceError(f"microphone capture failed: {exc}.") from exc
    finally:
        stopped.set()
        if worker is not None:
            worker.join(timeout=10.0)

    data = b"".join(frames)
    if not data:
        raise VoiceError("no audio was captured.")

    samples = numpy.frombuffer(data, dtype=numpy.int16).astype(numpy.float32) / 32768.0
    try:
        text = transcribe_audio(final_model, samples, language)
    except Exception as exc:
        raise VoiceError(f"`{model}` transcription failed: {exc}.") from exc

    if not text:
        raise VoiceError("transcription returned no text.")

    return text


def _stream_partials(
    whisper: object, frames: list[bytes], language: str | None, partial: "_Partial", stopped: threading.Event
) -> None:
    import numpy

    threshold = int(_SAMPLE_RATE * _SAMPLE_WIDTH * _MIN_PARTIAL_SECONDS)
    failures = 0
    while not stopped.wait(_PARTIAL_INTERVAL):
        data = b"".join(frames[:])
        if len(data) < threshold:
            continue

        samples = numpy.frombuffer(data, dtype=numpy.int16).astype(numpy.float32) / 32768.0
        try:
            hypothesis = transcribe_audio(whisper, samples, language)
        except Exception:
            failures += 1
            if failures >= 3:
                break
            continue

        failures = 0
        if hypothesis:
            partial.update(hypothesis)


class _Partial:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._previous: list[str] = []
        self._committed: list[str] = []
        self._tentative = ""

    def update(self, hypothesis: str) -> None:
        """Fold in a hypothesis, committing the prefix agreed by the last two, never retracting."""

        words = hypothesis.split()
        with self._lock:
            agreed = _common_word_count(self._previous, words)
            if agreed > len(self._committed):
                self._committed = words[:agreed]
            self._tentative = " ".join(words[len(self._committed) :])
            self._previous = words

    def snapshot(self) -> tuple[str, str]:
        """Return the committed and tentative transcript parts."""

        with self._lock:
            return " ".join(self._committed), self._tentative


def _common_word_count(previous: list[str], current: list[str]) -> int:
    count = 0
    for earlier, later in zip(previous, current):
        if earlier != later:
            break
        count += 1

    return count


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


def _wait_until_stopped(level: "_Level", partial: "_Partial") -> None:
    if not sys.stdout.isatty():
        input("🎙  Recording — press Enter to stop")
        return

    console = theme.err
    stopped = threading.Event()
    threading.Thread(target=_wait_for_enter, args=(stopped,), daemon=True).start()

    start = time.monotonic()
    with Live(console=console, refresh_per_second=12, transient=True) as live:
        while not stopped.is_set():
            live.update(_live_view(time.monotonic() - start, level, partial))
            time.sleep(0.08)

    console.print(f"[green]✓[/green] captured [bold]{_elapsed(time.monotonic() - start)}[/bold]")


def _wait_for_enter(stopped: threading.Event) -> None:
    try:
        input()
    except EOFError:
        pass

    stopped.set()


def _live_view(elapsed: float, level: "_Level", partial: "_Partial") -> Group:
    committed, tentative = partial.snapshot()

    body = Text()
    if committed:
        body.append(committed)
    if tentative:
        if committed:
            body.append(" ")
        body.append(tentative, style="dim italic")
    if not committed and not tentative:
        body.append("listening…", style="dim italic")

    return Group(_meter(elapsed, level.value), Text(""), body)


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
