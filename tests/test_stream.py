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
    def test_create_engine_from_config_laptop_mic_customized(self, mock_query):
        mock_query.return_value = {'name': 'Realtek(R) Audio Microphone Array', 'max_input_channels': 2, 'max_output_channels': 2}
        config = DEFAULT_CONFIG.copy()
        config["vad_threshold"] = 0.75
        config["vad_hangover_ms"] = 180.0
        config["vad_customized"] = True

        engine = create_engine_from_config(config, input_device_id=0, output_device_id=1, is_laptop_mic=True)
        # When explicitly customized by the user, 0.75 is NOT forced to 0.70
        self.assertEqual(engine.vad_threshold, 0.75)
        self.assertEqual(engine.vad_hangover_ms, 180.0)

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

    @patch("src.stream.sd.query_devices")
    @patch("src.devices.validate_device_index", return_value=None)
    @patch("src.devices.auto_select_input_device", return_value={'index': 3, 'name': 'Fallback Mic'})
    @patch("src.devices.auto_select_output_device", return_value=({'index': 4, 'name': 'Fallback Spk'}, 'Test Mode'))
    def test_create_engine_fallback_on_invalid_device(self, mock_auto_out, mock_auto_in, mock_val, mock_query):
        mock_query.return_value = {'name': 'Fallback Mic', 'max_input_channels': 1, 'max_output_channels': 2}
        config = DEFAULT_CONFIG.copy()
        engine = create_engine_from_config(config, input_device_id=99, output_device_id=98)
        self.assertEqual(engine.input_device, 3)
        self.assertEqual(engine.output_device, 4)

    @patch("src.stream.sd.query_devices")
    def test_prepare_for_stop_guarantees_stop_on_router_failure(self, mock_query):
        mock_query.return_value = {'name': 'Mock Mic', 'max_input_channels': 1, 'max_output_channels': 2}
        mock_router = MagicMock()
        mock_router.restore_original.side_effect = RuntimeError("COM Failure")

        engine = AudioEngine(input_device=0, output_device=1, router=mock_router)
        with patch.object(engine, "stop") as mock_stop:
            # Does not crash caller when router fails, and stop() is guaranteed to run
            engine.prepare_for_stop()
            mock_stop.assert_called_once()

    @patch("src.stream.sd.Stream")
    @patch("src.stream.sd.query_devices")
    def test_stream_start_auto_route_success(self, mock_query, mock_stream_cls):
        mock_query.return_value = {'name': 'Mock Mic', 'max_input_channels': 1, 'max_output_channels': 2}
        mock_router = MagicMock()
        mock_router.config = {"auto_route": True}
        mock_router.swap_to_cable.return_value = True

        engine = AudioEngine(input_device=0, output_device=1, router=mock_router)
        engine.start()
        self.assertTrue(engine.router_swap_success)
        mock_router.swap_to_cable.assert_called_once()

    @patch("src.stream.sd.Stream")
    @patch("src.stream.sd.query_devices")
    def test_stream_start_auto_route_failure(self, mock_query, mock_stream_cls):
        mock_query.return_value = {'name': 'Mock Mic', 'max_input_channels': 1, 'max_output_channels': 2}
        mock_router = MagicMock()
        mock_router.config = {"auto_route": True}
        mock_router.swap_to_cable.return_value = False

        engine = AudioEngine(input_device=0, output_device=1, router=mock_router)
        engine.start()
        self.assertFalse(engine.router_swap_success)
        mock_router.swap_to_cable.assert_called_once()

    @patch("src.stream.sd.Stream")
    @patch("src.stream.sd.query_devices")
    def test_stream_start_auto_route_exception(self, mock_query, mock_stream_cls):
        mock_query.return_value = {'name': 'Mock Mic', 'max_input_channels': 1, 'max_output_channels': 2}
        mock_router = MagicMock()
        mock_router.config = {"auto_route": True}
        mock_router.swap_to_cable.side_effect = RuntimeError("COM Policy Denied")

        engine = AudioEngine(input_device=0, output_device=1, router=mock_router)
        engine.start()
        self.assertFalse(engine.router_swap_success)
        mock_router.swap_to_cable.assert_called_once()


if __name__ == "__main__":
    unittest.main()
