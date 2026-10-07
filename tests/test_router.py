"""
Unit tests for SmartMicRouter, atomic config saving, and v1.2.0 routing features.
"""
import os
import json
import unittest
from unittest.mock import MagicMock, patch

from src.router import SmartMicRouter
from src.config import save_config, load_config
from src.devices import get_physical_output_device
from src.stream import AudioEngine


class TestSmartMicRouter(unittest.TestCase):
    def setUp(self):
        self.config = {
            "version": "1.2.1",
            "is_swapped": False,
            "original_mic_id": None,
        }
        self.save_patcher = patch("src.router.save_config")
        self.mock_save = self.save_patcher.start()

    def tearDown(self):
        self.save_patcher.stop()

    def test_router_init(self):
        router = SmartMicRouter(self.config)
        self.assertFalse(router.is_swapped)
        self.assertIsNone(router.original_mic_id)

    def test_audit_crash_recovery_not_swapped(self):
        router = SmartMicRouter(self.config)
        with patch.object(router, '_run_helper') as mock_run:
            recovered = router.audit_crash_recovery()
            self.assertFalse(recovered)
            mock_run.assert_not_called()

    def test_audit_crash_recovery_when_swapped(self):
        self.config["is_swapped"] = True
        self.config["original_mic_id"] = "{mock-guid}"
        router = SmartMicRouter(self.config)
        with patch.object(router, 'get_current_default_mic', return_value=("{cable-guid}", "CABLE Output")):
            with patch.object(router, '_run_helper', return_value=(0, "Restored")):
                recovered = router.audit_crash_recovery()
                self.assertTrue(recovered)
                self.assertFalse(self.config["is_swapped"])

    def test_cannot_swap_if_cable_missing(self):
        router = SmartMicRouter(self.config)
        with patch.object(router, 'get_cable_capture_guid', return_value=None):
            result = router.swap_to_cable()
            self.assertFalse(result)
            self.assertFalse(router.is_swapped)

    def test_auto_route_disabled(self):
        self.config["auto_route"] = False
        router = SmartMicRouter(self.config)
        with patch.object(router, '_run_helper') as mock_run:
            result = router.swap_to_cable()
            self.assertTrue(result)
            mock_run.assert_not_called()
            self.assertFalse(router.is_swapped)

    def test_restore_when_not_swapped(self):
        router = SmartMicRouter(self.config)
        with patch.object(router, '_run_helper') as mock_run:
            router.restore_original()
            mock_run.assert_not_called()

    def test_restore_success(self):
        self.config["is_swapped"] = True
        self.config["original_mic_id"] = "{original-guid}"
        router = SmartMicRouter(self.config)
        with patch.object(router, '_run_helper', return_value=(0, "Success")), \
             patch.object(router, '_set_capture_endpoint', return_value=True):
            res = router.restore_original()
            self.assertTrue(res)
            self.assertFalse(router.is_swapped)
            self.assertFalse(self.config["is_swapped"])

    def test_restore_failure(self):
        self.config["is_swapped"] = True
        self.config["original_mic_id"] = "{original-guid}"
        router = SmartMicRouter(self.config)
        with patch.object(router, '_run_helper', return_value=(1, "Failed")), \
             patch.object(router, '_set_capture_endpoint', return_value=False):
            res = router.restore_original()
            self.assertFalse(res)

    def test_audit_crash_recovery_failure(self):
        self.config["is_swapped"] = True
        self.config["original_mic_id"] = "{mock-guid}"
        router = SmartMicRouter(self.config)
        with patch.object(router, 'get_current_default_mic', return_value=("{cable-guid}", "CABLE Output")):
            with patch.object(router, '_set_capture_endpoint', return_value=False), \
                 patch.object(router, '_run_helper', return_value=(1, "Failed")):
                recovered = router.audit_crash_recovery()
                self.assertFalse(recovered)
                self.assertTrue(self.config["is_swapped"])

    def test_swap_to_cable_inconclusive_rolls_back(self):
        router = SmartMicRouter(self.config)
        with patch.object(router, 'get_cable_capture_guid', return_value="{cable-guid-123}"), \
             patch.object(router, 'get_current_default_mic', side_effect=[
                 ("{physical-guid-456}", "Microphone (Realtek)"),
                 ("{physical-guid-456}", "Microphone (Realtek)")  # post-swap still on physical!
             ]), \
             patch.object(router, '_set_capture_endpoint', return_value=True):
            result = router.swap_to_cable()
            self.assertFalse(result)
            self.assertFalse(router.is_swapped)
            self.assertFalse(self.config["is_swapped"])

    def test_swap_to_cable_native_success(self):
        router = SmartMicRouter(self.config)
        with patch.object(router, 'get_cable_capture_guid', return_value="{cable-guid-123}"), \
             patch.object(router, 'get_current_default_mic', side_effect=[
                 ("{physical-guid-456}", "Microphone (Realtek)"),
                 ("{cable-guid-123}", "CABLE Output (VB-Audio Virtual Cable)")
             ]), \
             patch("src.router.native_set_default_endpoint", return_value=True):
            result = router.swap_to_cable()
            self.assertTrue(result)
            self.assertTrue(router.is_swapped)
            self.assertTrue(self.config["is_swapped"])
            self.assertEqual(self.config["original_mic_id"], "{physical-guid-456}")

    def test_restore_original_native_success(self):
        self.config["is_swapped"] = True
        self.config["original_mic_id"] = "{physical-guid-456}"
        router = SmartMicRouter(self.config)
        with patch("src.router.native_set_default_endpoint", return_value=True):
            result = router.restore_original()
            self.assertTrue(result)
            self.assertFalse(router.is_swapped)
            self.assertFalse(self.config["is_swapped"])


class TestAtomicConfig(unittest.TestCase):
    def test_save_and_load_config(self):
        cfg = load_config()
        self.assertIsInstance(cfg, dict)
        self.assertIn("version", cfg)
        self.assertEqual(cfg["version"], "1.2.8")


        # Test atomic save
        cfg["test_key"] = "test_val_123"
        save_config(cfg)

        reloaded = load_config()
        self.assertEqual(reloaded.get("test_key"), "test_val_123")

        # Clean up test keys and leave pristine config.json
        reloaded.pop("test_key", None)
        reloaded["is_swapped"] = False
        reloaded["original_mic_id"] = None
        save_config(reloaded)


class TestAudioEngineV120(unittest.TestCase):
    def test_set_vad_threshold(self):
        engine = AudioEngine(
            input_device=0,
            output_device=0,
            vad_threshold=0.75,
        )
        self.assertEqual(engine.vad_threshold, 0.75)
        self.assertEqual(engine.gate.threshold, 0.75)

        engine.set_vad_threshold(0.85)
        self.assertEqual(engine.vad_threshold, 0.85)
        self.assertEqual(engine.gate.threshold, 0.85)

    def test_prepare_for_stop(self):
        mock_router = MagicMock()
        engine = AudioEngine(
            input_device=0,
            output_device=0,
            router=mock_router,
        )
        engine.prepare_for_stop()
        mock_router.restore_original.assert_called_once()
        self.assertFalse(engine.is_active())

    def test_smart_mic_routing_full_cycle(self):
        mock_router = MagicMock()
        mock_router.config = {"auto_route": True}
        mock_router.swap_to_cable.return_value = True
        mock_router.restore_original.return_value = True

        engine = AudioEngine(
            input_device=0,
            output_device=0,
            router=mock_router,
        )

        with patch("sounddevice.Stream") as mock_stream_cls, \
             patch("sounddevice.query_devices", return_value={"name": "MockDev", "max_input_channels": 2, "max_output_channels": 2}):
            mock_stream_instance = MagicMock()
            mock_stream_cls.return_value = mock_stream_instance

            # 1. Start stream and verify swap_to_cable called
            engine.start()
            self.assertTrue(engine.is_active())
            mock_router.swap_to_cable.assert_called_once()

            # 2. Stop stream and verify restore_original called
            engine.prepare_for_stop()
            self.assertFalse(engine.is_active())
            mock_router.restore_original.assert_called_once()


class TestPhysicalOutput(unittest.TestCase):
    def test_get_physical_output(self):
        out = get_physical_output_device()
        if out is not None:
            self.assertIn("name", out)
            self.assertNotIn("cable input", out["name"].lower())
            self.assertNotIn("cable output", out["name"].lower())


class TestAutoRouteToggleLock(unittest.TestCase):
    def test_auto_route_toggle_locked_when_active(self):
        mock_gui = MagicMock()
        mock_gui.is_running = True
        mock_gui._is_testing_voice = False
        mock_gui.config = {"auto_route": True}
        import threading
        mock_gui._state_lock = threading.Lock()

        from src.gui import GetsuGUI
        with patch("src.gui.dpg.does_item_exist", return_value=True), \
             patch("src.gui.dpg.set_value") as mock_set_val:
            GetsuGUI.on_auto_route_toggled(mock_gui, "chk_auto_route", False)
            mock_set_val.assert_called_with("chk_auto_route", True)
            mock_gui.set_status_pill.assert_called_once_with(
                "▲ Stop noise cancellation first to change routing preference.", [240, 180, 50]
            )
            self.assertTrue(mock_gui.config["auto_route"])

    def test_auto_route_toggle_allowed_when_idle(self):
        mock_gui = MagicMock()
        mock_gui.is_running = False
        mock_gui._is_testing_voice = False
        mock_gui.config = {"auto_route": True}
        import threading
        mock_gui._state_lock = threading.Lock()
        mock_gui._get_idle_status.return_value = "Ready"
        mock_gui._get_idle_color.return_value = [100, 100, 100]

        from src.gui import GetsuGUI
        with patch("src.gui.save_config") as mock_save:
            GetsuGUI.on_auto_route_toggled(mock_gui, "chk_auto_route", False)
            self.assertFalse(mock_gui.config["auto_route"])
            mock_save.assert_called_once()
            mock_gui.set_status_pill.assert_called_with("Ready", [100, 100, 100])


if __name__ == "__main__":
    unittest.main()
