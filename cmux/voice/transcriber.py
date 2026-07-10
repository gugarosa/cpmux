# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

from pathlib import Path

DEFAULT_TRANSCRIBE_MODEL = "whisper-large-v3"


class VoiceError(Exception):
    """Raised when recording, transcription, or plan synthesis fails."""


def transcribe(audio_path: str | Path, model: str = DEFAULT_TRANSCRIBE_MODEL, endpoint: str | None = None) -> str:
    """Transcribe an audio file to text via Foundry Local's Whisper endpoint.

    Args:
        audio_path: Audio file to transcribe.
        model: Foundry Local audio model alias to run.
        endpoint: OpenAI-compatible base URL, discovered via the SDK when omitted.

    Returns:
        The transcribed text.

    Raises:
        VoiceError: If Foundry Local is unavailable or the request fails.

    """

    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise VoiceError(f"`{audio_path}` audio file does not exist.")

    base_url, api_key = _resolve_endpoint(model, endpoint)

    try:
        import httpx
    except ImportError as exc:
        raise VoiceError("transcription needs `httpx`, install `cmux[voice]`.") from exc

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with audio_path.open("rb") as handle:
            response = httpx.post(
                f"{base_url}/audio/transcriptions",
                headers=headers,
                files={"file": (audio_path.name, handle, "application/octet-stream")},
                data={"model": model},
                timeout=300,
            )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise VoiceError(f"`{model}` transcription request failed: {exc}.") from exc

    text = str(response.json().get("text", "")).strip()
    if not text:
        raise VoiceError("transcription returned no text.")

    return text


def _resolve_endpoint(model: str, endpoint: str | None) -> tuple[str, str | None]:
    if endpoint:
        return endpoint.rstrip("/"), None

    try:
        from foundry_local import FoundryLocalManager
    except ImportError as exc:
        raise VoiceError("transcription needs Foundry Local, install `cmux[voice]` or pass `--endpoint`.") from exc

    try:
        manager = FoundryLocalManager(model)
    except Exception as exc:
        raise VoiceError(f"could not start Foundry Local for `{model}`: {exc}.") from exc

    return manager.endpoint.rstrip("/"), manager.api_key
