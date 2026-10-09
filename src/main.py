"""
Getsu - Real-Time AI Noise Cancellation Application.
Main entry point orchestrating GUI, device checks, audio engine, and system tray.
"""
import os
import sys
import time
import signal
import argparse

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Fast Single-Instance Micro-Checker (exits in <5ms if Getsu is already running)
if not any(arg in sys.argv for arg in ("--check-devices", "--install-driver", "-h", "--help", "--cli", "--clean", "--batch")):
    from src.single_instance import activate_existing_instance
    if activate_existing_instance():
        sys.exit(0)

# Direct delegate to Headless CLI Cleaner if invoked with --clean or --batch
if any(arg in sys.argv for arg in ("--clean", "--batch")):
    from src.cli_cleaner import main as cli_cleaner_main
    sys.exit(cli_cleaner_main())

from src.devices import (
    print_device_report,
    auto_select_input_device,
    auto_select_output_device,
    check_vbcable_status,
    install_vbcable_driver,
)
from src.config import load_config, save_config
from src.stream import AudioEngine, create_engine_from_config
from src.gui import launch_gui


def parse_args():
    parser = argparse.ArgumentParser(description="Getsu - Real-Time AI Noise Filter")
    parser.add_argument("--cli", action="store_true", help="Run in interactive CLI mode without GUI")
    parser.add_argument("--tray", action="store_true", help="Run in background system tray mode")
    parser.add_argument("--check-devices", action="store_true", help="Print audio device diagnostics and exit")
    parser.add_argument("--install-driver", action="store_true", help="Install bundled VB-Audio Virtual Cable driver")
    return parser.parse_args()


def run_tray_mode():
    from src.tray import TrayApp
    from src.router import SmartMicRouter
    config = load_config()
    _, def_in, def_out = print_device_report()
    input_device_id = config.get("input_device_id", def_in['index'] if def_in else None)
    output_device_id = config.get("output_device_id", def_out['index'] if def_out else None)

    if input_device_id is None:
        print("[ERROR] No audio input device (microphone) detected. Exiting tray mode.")
        return

    router = SmartMicRouter(config)
    engine = create_engine_from_config(config, input_device_id, output_device_id, router=router)
    engine.start()

    print("\n[GETSU] Starting Windows System Tray...")
    tray = TrayApp(engine, config)
    tray.run()


def run_cli_mode():
    from src.router import SmartMicRouter
    config = load_config()
    _, def_in, def_out = print_device_report()
    input_device_id = config.get("input_device_id", def_in['index'] if def_in else None)
    output_device_id = config.get("output_device_id", def_out['index'] if def_out else None)

    if input_device_id is None:
        print("[ERROR] No audio input device (microphone) detected. Exiting CLI mode.")
        return

    router = SmartMicRouter(config)
    engine = create_engine_from_config(config, input_device_id, output_device_id, router=router)
    engine.start()

    def shutdown(sig=None, frame=None):
        print("\n[GETSU] Stopping audio engine...")
        engine.prepare_for_stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    print("\n[GETSU] Running in CLI Monitor Mode (Press Ctrl+C to stop)")
    print("Cooling Pad Filter: ON | AI Denoise: ON | Buffer: 10ms")
    print("-" * 65)
    try:
        while True:
            time.sleep(0.2)
            prob = engine.last_speech_prob
            peak = engine.last_peak_db
            state_str = "MUTED" if engine.is_muted else ("SPEECH" if prob > 0.5 else "SILENCE (NOISE CLAMPED)")
            bars = int(max(0, (peak + 60) / 2))
            meter = "#" * bars + " " * (30 - bars)
            print(f"\r [{state_str:<23}] Level: [{meter}] {peak:5.1f} dBFS | Voice Prob: {prob:4.2f}", end="", flush=True)
    except KeyboardInterrupt:
        shutdown()


def main():
    args = parse_args()

    # Startup Audit: Auto-recover Windows default mic if previous session ended in unclean shutdown
    try:
        from src.router import SmartMicRouter
        initial_config = load_config()
        recovery_router = SmartMicRouter(initial_config)
        recovery_router.audit_crash_recovery()
    except Exception as e:
        print(f"[RECOVERY] Warning: Startup audio audit encountered error: {e}")

    if args.check_devices:
        print_device_report()
        sys.exit(0)

    if args.install_driver:
        print("[SETUP] Installing VB-Audio Virtual Cable driver...")
        success = install_vbcable_driver()
        if success:
            print("[SETUP] Success! Please restart your computer or reload devices.")
        else:
            print("[SETUP] Installation cancelled or failed.")
        sys.exit(0)

    if args.cli:
        run_cli_mode()
    elif args.tray:
        run_tray_mode()
    else:
        # Default: Launch the sleek GUI with Start/Stop & Input Selector
        launch_gui()


if __name__ == "__main__":
    main()
