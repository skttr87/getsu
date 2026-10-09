"""
Unit Tests for Getsu Headless CLI Batch Audio Processor.
Tests sample-accurate duration matching, 44.1kHz -> 48kHz resampling, linked stereo,
overwrite guards, and telemetry reporting.
"""
import os
import wave
import json
import tempfile
import unittest
import numpy as np

from src.cli_cleaner import clean_file, clean_batch


class TestCLIBatchCleaner(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.dir_path = self.tmp_dir.name

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _create_wav(self, filename: str, sample_rate: int = 48000, nchannels: int = 1, duration_sec: float = 0.5) -> str:
        fpath = os.path.join(self.dir_path, filename)
        num_samples = int(sample_rate * duration_sec)
        t = np.linspace(0, duration_sec, num_samples, False)
        # 400 Hz tone + minor noise
        audio = 0.3 * np.sin(2 * np.pi * 400 * t).astype(np.float32)
        if nchannels == 2:
            audio = np.column_stack([audio, audio * 0.8])
        pcm_data = (audio * 32767.0).astype(np.int16)

        with wave.open(fpath, "wb") as wf:
            wf.setnchannels(nchannels)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_data.tobytes())

        return fpath

    def test_sample_accurate_duration_matching(self):
        """Verify output audio matches input sample count down to the exact sample (zero-flush latency extraction)."""
        in_file = self._create_wav("test_48k_mono.wav", sample_rate=48000, nchannels=1, duration_sec=1.0)
        out_file = os.path.join(self.dir_path, "out_48k_mono.wav")

        stats = clean_file(in_file, out_file, progress=False)

        self.assertEqual(stats["samples_in"], 48000)
        self.assertEqual(stats["samples_out"], 48000)
        self.assertTrue(os.path.isfile(out_file))

        with wave.open(out_file, "rb") as wf:
            self.assertEqual(wf.getnframes(), 48000)
            self.assertEqual(wf.getframerate(), 48000)
            self.assertEqual(wf.getnchannels(), 1)

    def test_universal_44100_to_48000_resampling(self):
        """Verify 44.1 kHz input file is cleanly resampled to 48 kHz output with accurate duration."""
        in_file = self._create_wav("test_44k_mono.wav", sample_rate=44100, nchannels=1, duration_sec=1.0)
        out_file = os.path.join(self.dir_path, "out_resampled.wav")

        stats = clean_file(in_file, out_file, progress=False)

        self.assertTrue(os.path.isfile(out_file))
        with wave.open(out_file, "rb") as wf:
            self.assertEqual(wf.getframerate(), 48000)
            # Duration should be approximately 1.0 second (48000 frames)
            self.assertAlmostEqual(wf.getnframes() / 48000.0, 1.0, delta=0.01)

    def test_linked_stereo_processing(self):
        """Verify 2-channel stereo file processes cleanly with linked-stereo gating."""
        in_file = self._create_wav("test_stereo.wav", sample_rate=48000, nchannels=2, duration_sec=0.8)
        out_file = os.path.join(self.dir_path, "out_stereo.wav")

        stats = clean_file(in_file, out_file, stereo=True, progress=False)

        self.assertTrue(os.path.isfile(out_file))
        with wave.open(out_file, "rb") as wf:
            self.assertEqual(wf.getnchannels(), 2)
            self.assertEqual(wf.getnframes(), int(48000 * 0.8))

    def test_destructive_overwrite_guard(self):
        """Verify ValueError is raised if input and output resolve to the exact same file."""
        in_file = self._create_wav("same_name.wav", sample_rate=48000, nchannels=1, duration_sec=0.2)

        with self.assertRaises(ValueError):
            clean_file(in_file, in_file, progress=False)

    def test_report_json_telemetry_dump(self):
        """Verify --report-json generates valid JSON telemetry with expected keys."""
        in_file = self._create_wav("test_telemetry.wav", sample_rate=48000, nchannels=1, duration_sec=0.3)
        out_file = os.path.join(self.dir_path, "out_telemetry.wav")
        json_file = os.path.join(self.dir_path, "report.json")

        clean_file(in_file, out_file, report_json=json_file, progress=False)

        self.assertTrue(os.path.isfile(json_file))
        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIn("samples_processed", data)
        self.assertIn("speed_factor", data)
        self.assertIn("frames", data)
        self.assertGreater(len(data["frames"]), 0)
        self.assertIn("speech_prob", data["frames"][0])
        self.assertIn("rms_db", data["frames"][0])

    def test_clean_batch_directory(self):
        """Verify clean_batch discovers and processes all WAV files in a directory."""
        self._create_wav("batch_1.wav", duration_sec=0.2)
        self._create_wav("batch_2.wav", duration_sec=0.2)
        out_sub = os.path.join(self.dir_path, "cleaned_out")

        results = clean_batch(self.dir_path, output_dest=out_sub, progress=False)

        self.assertEqual(len(results), 2)
        self.assertTrue(os.path.isfile(os.path.join(out_sub, "batch_1_cleaned.wav")))
        self.assertTrue(os.path.isfile(os.path.join(out_sub, "batch_2_cleaned.wav")))


if __name__ == "__main__":
    unittest.main()

