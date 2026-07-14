# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import sys

import pytest

from cpmux.voice.recorder import record_and_transcribe
from cpmux.voice.transcriber import VoiceError


def test_record_and_transcribe_reports_missing_backend(monkeypatch):
    monkeypatch.setitem(sys.modules, "numpy", None)
    with pytest.raises(VoiceError):
        record_and_transcribe("base")
