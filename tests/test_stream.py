import unittest
from unittest.mock import MagicMock, patch
import numpy as np

from src.stream import AudioEngine, create_engine_from_config
from src.config import DEFAULT_CONFIG


class TestAudioEngine(unittest.TestCase):
    @patch("src.stream.sd.query_devices")
    def test_metrics_properties(self, mock_query):
        mock_query.return_value = {'name': 'Mock Mic', 'max_input_channels': 1, 'max_output_channels': 2}
        engine = AudioEngine(input_device=0, output_device=1)

        # Initial metrics
        self.assertEqual(engine.last_speech_prob, 0.0)
        self.assertEqual(engine.last_peak_db, -100.0)
        self.assertEqual(engine.last_rms_db, -100.0)
        self.assertEqual(engine.last_input_peak_db, -100.0)

        # Mutate internal tuple atomically
        engine._metrics = (0.92, -12.5, -18.2, -10.0)
        self.assertEqual(engine.last_speech_prob, 0.92)
        self.assertEqual(engine.last_peak_db, -12.5)
        self.assertEqual(engine.last_rms_db, -18.2)
        self.assertEqual(engine.last_input_peak_db, -10.0)

    @patch("src.stream.sd.query_devices")
    def test_create_engine_from_config_laptop_mic(self, mock_query):
        mock_query.return_value = {'name': 'Realtek(R) Audio Microphone Array', 'max_input_channels': 2, 'max_output_channels': 2}
        config = DEFAULT_CONFIG.copy()
        config["mic_boost_db"] = 5

        engine = create_engine_from_config(config, input_device_id=0, output_device_id=1, is_laptop_mic=True)
        self.assertEqual(engine.vad_threshold, 0.70)
        self.assertEqual(engine.vad_hangover_ms, 220.0)
        # base_gain (1.2) * 10^(5/20) (1.778) ≈ 2.134
        self.assertAlmostEqual(engine.mic_gain, 1.2 * (10.0 ** (5 / 20.0)), places=2)
        self.assertEqual(engine.output_gain, 1.08)
        self.assertEqual(engine.hpf_cutoff_hz, 80.0)

    @patch("src.stream.sd.query_devices")
    def test_create_engine_from_config_external_mic(self, mock_query):
        mock_query.return_value = {'name': 'HyperX QuadCast', 'max_input_channels': 2, 'max_output_channels': 2}
        config = DEFAULT_CONFIG.copy()
        config["mic_boost_db"] = 0

        engine = create_engine_from_config(config, input_device_id=0, output_device_id=1, is_laptop_mic=False)
        self.assertEqual(engine.vad_threshold, 0.75)
        self.assertEqual(engine.vad_hangover_ms, 180.0)
        self.assertEqual(engine.mic_gain, 1.0)

    @patch("src.stream.sd.query_devices")
    def test_is_stream_alive(self, mock_query):
        mock_query.return_value = {'name': 'Mock Mic', 'max_input_channels': 1, 'max_output_channels': 2}
        engine = AudioEngine(input_device=0, output_device=1)

        # 1. Not running
        self.assertFalse(engine.is_stream_alive())

        # 2. Running and active
        engine._running = True
        mock_stream = MagicMock()
        mock_stream.active = True
        engine._stream = mock_stream
        engine._last_callback_time = 100.0

        with patch("src.stream.time.monotonic", return_value=100.5):
            self.assertTrue(engine.is_stream_alive())

        # 3. Stream marked inactive by PortAudio
        mock_stream.active = False
        with patch("src.stream.time.monotonic", return_value=100.5):
            self.assertFalse(engine.is_stream_alive())

        # 4. Stream stalled: callback > 2.0s ago
        mock_stream.active = True
        with patch("src.stream.time.monotonic", return_value=103.0):
            self.assertFalse(engine.is_stream_alive())


if __name__ == "__main__":
    unittest.main()
