"""Regression tests: a persistent InputStream goes dead (delivers pure digital
silence, peak RMS 0) after macOS reconfigures audio devices — e.g. AirPods
connecting while Hermes is running. PortAudio caches the device list at init,
so the recorder must close the stream, re-initialise PortAudio, and reselect
the input device on the next ``start()``.
"""

from unittest.mock import MagicMock

import pytest

from tools.voice_mode import AudioRecorder


class _Frame:
    def __init__(self, n):
        self._n = n

    def __len__(self):
        return self._n

    def tobytes(self):
        return b"\x00\x00" * self._n


class _FakeNp:
    def concatenate(self, frames, axis=0):
        return frames[0]


@pytest.fixture
def fake_sd(monkeypatch, tmp_path):
    sd = MagicMock()
    sd.query_devices.return_value = {"default_samplerate": 16000}
    monkeypatch.setattr("tools.voice_mode._import_audio", lambda: (sd, _FakeNp()))
    monkeypatch.setattr("tools.voice_mode._TEMP_DIR", str(tmp_path))
    monkeypatch.setattr("tools.voice_mode._select_input_device", lambda: 2)
    monkeypatch.setattr(AudioRecorder, "_open_live_session", lambda self, rate: None)
    return sd


def _arm(recorder, *, peak_rms, samples=16000):
    recorder._recording = True
    recorder._frames = [_Frame(samples)]
    recorder._peak_rms = peak_rms


class TestDeadStreamRefresh:

    def test_dead_stream_is_rebuilt_with_fresh_device_list_on_next_start(self, fake_sd):
        # Arrange: a recording that captured pure digital silence (dead stream)
        recorder = AudioRecorder()
        dead_stream = MagicMock()
        dead_stream.active = True  # dead CoreAudio streams still report active
        recorder._stream = dead_stream
        _arm(recorder, peak_rms=0)
        assert recorder.stop() is None

        # Act
        recorder.start()

        # Assert: PortAudio re-initialised and a new stream opened
        dead_stream.close.assert_called()
        fake_sd._terminate.assert_called_once()
        fake_sd._initialize.assert_called_once()
        fake_sd.InputStream.assert_called_once()
        assert recorder._stream is not dead_stream

    def test_quiet_but_live_stream_is_reused(self, fake_sd):
        # Arrange: quiet room — below threshold but not digital silence
        recorder = AudioRecorder()
        live_stream = MagicMock()
        live_stream.active = True
        recorder._stream = live_stream
        _arm(recorder, peak_rms=50)
        assert recorder.stop() is None

        # Act
        recorder.start()

        # Assert
        fake_sd._terminate.assert_not_called()
        fake_sd.InputStream.assert_not_called()
        assert recorder._stream is live_stream
