"""
Getsu Headless Audio Cleaner & Batch CLI Processor.
Universal format decoding via miniaudio, 48kHz resampling, sample-accurate duration trimming,
zero-flush latency extraction, linked-stereo gating, and TPDF dithered 16-bit PCM WAV export.
"""
import os
import sys
import time
import math
import json
import glob
import wave
import argparse
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
import numpy as np
import miniaudio

from src.rnnoise import RNNoise, FRAME_SIZE, SAMPLE_RATE
from src.dsp import (
    HighPassFilter,
    AdaptiveNoiseGate,
    calculate_levels,
    soft_limit,
    soft_preclip,
    SpeechLeveler,
    TransientSuppressor,
    process_mono_frame,
)


def print_progress_bar(current: int, total: int, prefix: str = "", bar_length: int = 30):
    """Dynamic terminal progress bar."""
    if total <= 0:
        return
    fraction = min(1.0, current / total)
    filled = int(round(bar_length * fraction))
    bar = "█" * filled + "░" * (bar_length - filled)
    percent = fraction * 100.0
    sys.stdout.write(f"\r{prefix} |{bar}| {percent:5.1f}% [{current}/{total}]")
    sys.stdout.flush()
    if current >= total:
        sys.stdout.write("\n")
        sys.stdout.flush()


def clean_file(
    input_path: str | Path,
    output_path: Optional[str | Path] = None,
    no_rnnoise: bool = False,
    no_gate: bool = False,
    auto_level: bool = False,
    music_mode: bool = False,
    gain_db: float = 0.0,
    stereo: bool = False,
    progress: bool = True,
    report_json: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """
    Cleans a single audio file with sample-accurate duration matching.
    """
    input_p = Path(input_path).resolve()
    if not input_p.is_file():
        raise FileNotFoundError(f"Input file not found: {input_p}")

    if output_path is None:
        output_p = input_p.parent / f"{input_p.stem}_cleaned.wav"
    else:
        output_p = Path(output_path).resolve()

    if input_p == output_p:
        raise ValueError(f"Output file path cannot be identical to input file path: {input_p}")

    output_p.parent.mkdir(parents=True, exist_ok=True)

    t_start = time.perf_counter()

    # 1. Universal Decode & Resample to 48kHz float32
    target_channels = 2 if stereo else 1
    decoded = miniaudio.decode_file(
        str(input_p),
        output_format=miniaudio.SampleFormat.FLOAT32,
        nchannels=target_channels,
        sample_rate=SAMPLE_RATE,
    )
    raw_samples = np.frombuffer(decoded.samples, dtype=np.float32)
    total_frames_in = len(raw_samples) // target_channels
    if total_frames_in == 0:
        raise ValueError(f"Audio file contains zero readable audio frames: {input_p}")

    if stereo:
        audio_in = raw_samples.reshape(-1, 2)
    else:
        audio_in = raw_samples.reshape(-1, 1)

    # 2. Setup DSP Pipeline
    linear_gain = float(10.0 ** (gain_db / 20.0))
    use_rnnoise = (not no_rnnoise) and (not music_mode)
    use_gate = not no_gate

    # Initialize filters per channel
    hpf_list = [HighPassFilter(cutoff_hz=80.0, sample_rate=float(SAMPLE_RATE)) for _ in range(target_channels)]
    ts_list = [TransientSuppressor() for _ in range(target_channels)]

    rn_list = None
    if use_rnnoise:
        try:
            rn_list = [RNNoise() for _ in range(target_channels)]
        except Exception as e:
            print(f"[WARN] Failed to initialize RNNoise ({e}). Operating in bypass mode.")
            use_rnnoise = False

    gate_list = None
    if use_gate:
        hangover = 800.0 if music_mode else 320.0
        decay = 250.0 if music_mode else 80.0
        gate_list = [
            AdaptiveNoiseGate(
                threshold=0.70,
                close_threshold=0.52,
                hangover_ms=hangover,
                decay_ms=decay,
                lookahead=True,
                calibrate_startup=False,
            )
            for _ in range(target_channels)
        ]

    leveler_list = [SpeechLeveler() for _ in range(target_channels)] if auto_level else None

    # Algorithmic delay calculation
    # RNNoise: 1 frame (480 samples). Lookahead gate: 1 frame (480 samples).
    delay_samples = 0
    if use_rnnoise:
        delay_samples += FRAME_SIZE
    if use_gate:
        delay_samples += FRAME_SIZE
    delay_frames = delay_samples // FRAME_SIZE

    # Pad input to integer multiple of 480 samples + zero flush frames
    rem = total_frames_in % FRAME_SIZE
    pad_frames = (FRAME_SIZE - rem) if rem != 0 else 0
    total_work_samples = total_frames_in + pad_frames + (delay_frames * FRAME_SIZE)

    padded_in = np.zeros((total_work_samples, target_channels), dtype=np.float32)
    padded_in[:total_frames_in] = audio_in

    num_chunks = total_work_samples // FRAME_SIZE
    out_chunks = []
    telemetry = []

    # Calculate input statistics on first pass
    in_peak_db, in_rms_db = calculate_levels(audio_in[:, 0] if not stereo else (audio_in[:, 0] + audio_in[:, 1]) * 0.5)

    # 3. Stream processing
    for c_idx in range(num_chunks):
        start_i = c_idx * FRAME_SIZE
        end_i = start_i + FRAME_SIZE
        chunk = padded_in[start_i:end_i]

        if not stereo:
            # Mono processing
            mono_frame = chunk[:, 0].copy()
            rn_inst = rn_list[0] if rn_list else None
            gate_inst = gate_list[0] if gate_list else None
            lvl_inst = leveler_list[0] if leveler_list else None
            ts_inst = ts_list[0]

            if gate_inst is not None:
                proc, sp, pk, rms = process_mono_frame(
                    frame_mono=mono_frame,
                    rnnoise=rn_inst,
                    hpf=hpf_list[0],
                    gate=gate_inst,
                    total_gain=linear_gain,
                    denoise_enabled=bool(rn_inst is not None),
                    leveler=lvl_inst,
                    transient_suppressor=ts_inst,
                    mic_boost_db=0.0,
                )
            else:
                # Bypass gate
                if hpf_list[0]:
                    mono_frame = hpf_list[0].process(mono_frame)
                _, rms = calculate_levels(mono_frame)
                mono_frame = soft_preclip(mono_frame, knee=0.75)
                np.clip(mono_frame, -1.0, 1.0, out=mono_frame)
                sp = 0.0
                if rn_inst:
                    frn, sp = rn_inst.process_frame(mono_frame * 32767.0)
                    mono_frame = frn / 32767.0
                mono_frame = ts_inst.process(mono_frame, sp)
                if lvl_inst:
                    mono_frame, _ = lvl_inst.process(mono_frame, sp, rms)
                mono_frame *= linear_gain
                proc = soft_limit(mono_frame, threshold=0.85)

            out_chunks.append(proc.reshape(-1, 1))
            if report_json:
                telemetry.append({
                    "frame_idx": c_idx,
                    "speech_prob": round(float(sp), 4),
                    "rms_db": round(float(rms), 2),
                })
        else:
            # Coupled Linked-Stereo Processing
            ch0 = chunk[:, 0].copy()
            ch1 = chunk[:, 1].copy()

            # HPF & Preclip
            ch0 = hpf_list[0].process(ch0)
            ch1 = hpf_list[1].process(ch1)
            ch0 = soft_preclip(ch0, knee=0.75)
            ch1 = soft_preclip(ch1, knee=0.75)

            _, rms0 = calculate_levels(ch0)
            _, rms1 = calculate_levels(ch1)
            master_rms = max(rms0, rms1)

            # RNNoise per-channel with coupled speech probability
            sp0, sp1 = 0.0, 0.0
            if rn_list:
                frn0, sp0 = rn_list[0].process_frame(ch0 * 32767.0)
                ch0 = frn0 / 32767.0
                frn1, sp1 = rn_list[1].process_frame(ch1 * 32767.0)
                ch1 = frn1 / 32767.0
                master_sp = max(sp0, sp1)
            else:
                master_sp = min(1.0, max(0.0, (master_rms + 45.0) / 20.0))

            ch0 = ts_list[0].process(ch0, master_sp)
            ch1 = ts_list[1].process(ch1, master_sp)

            if leveler_list:
                ch0, _ = leveler_list[0].process(ch0, master_sp, master_rms)
                ch1, _ = leveler_list[1].process(ch1, master_sp, master_rms)

            ch0 *= linear_gain
            ch1 *= linear_gain

            if gate_list:
                # Coupled linked-stereo gating
                ch0, _ = gate_list[0].process(ch0, master_sp, input_rms_db=master_rms, in_place=True)
                ch1, _ = gate_list[1].process(ch1, master_sp, input_rms_db=master_rms, in_place=True)

            ch0 = soft_limit(ch0, threshold=0.85)
            ch1 = soft_limit(ch1, threshold=0.85)

            out_chunks.append(np.column_stack([ch0, ch1]))
            if report_json:
                telemetry.append({
                    "frame_idx": c_idx,
                    "speech_prob": round(float(master_sp), 4),
                    "rms_db": round(float(master_rms), 2),
                })

        if progress and (c_idx % 10 == 0 or c_idx == num_chunks - 1):
            print_progress_bar(c_idx + 1, num_chunks, prefix=f"Cleaning {input_p.name[:20]}")

    # 4. Latency Discard and Exact Duration Trimming
    concatenated = np.vstack(out_chunks)
    trimmed_audio = concatenated[delay_samples : delay_samples + total_frames_in]

    # 5. TPDF Dither & 16-bit PCM Quantization
    dither = (np.random.random(trimmed_audio.size) - np.random.random(trimmed_audio.size)).astype(np.float32) / 32768.0
    dithered = trimmed_audio.flatten() + dither
    pcm_int16 = np.clip(np.round(dithered * 32767.0), -32768, 32767).astype(np.int16)

    # 6. Export WAV
    with wave.open(str(output_p), "wb") as wf:
        wf.setnchannels(target_channels)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_int16.tobytes())

    t_end = time.perf_counter()
    elapsed_sec = max(1e-4, t_end - t_start)
    audio_dur_sec = total_frames_in / float(SAMPLE_RATE)
    speed_factor = audio_dur_sec / elapsed_sec

    out_peak_db, out_rms_db = calculate_levels(trimmed_audio[:, 0] if not stereo else (trimmed_audio[:, 0] + trimmed_audio[:, 1]) * 0.5)

    if report_json:
        report_p = Path(report_json).resolve()
        report_p.parent.mkdir(parents=True, exist_ok=True)
        with open(report_p, "w", encoding="utf-8") as f:
            json.dump({
                "input_file": str(input_p),
                "output_file": str(output_p),
                "samples_processed": total_frames_in,
                "duration_seconds": round(audio_dur_sec, 3),
                "speed_factor": round(speed_factor, 2),
                "frames": telemetry,
            }, f, indent=2)

    return {
        "input_file": str(input_p),
        "output_file": str(output_p),
        "samples_in": total_frames_in,
        "samples_out": len(trimmed_audio),
        "duration_sec": audio_dur_sec,
        "elapsed_sec": elapsed_sec,
        "speed_factor": speed_factor,
        "in_peak_db": in_peak_db,
        "in_rms_db": in_rms_db,
        "out_peak_db": out_peak_db,
        "out_rms_db": out_rms_db,
    }


def clean_batch(
    target_path: str | Path,
    output_dest: Optional[str | Path] = None,
    **kwargs,
) -> List[Dict[str, Any]]:
    """Processes a single file or an entire directory of audio files."""
    p = Path(target_path).resolve()
    results = []

    if p.is_file():
        res = clean_file(p, output_dest, **kwargs)
        results.append(res)
    elif p.is_dir():
        extensions = ("*.wav", "*.mp3", "*.flac", "*.ogg")
        files = []
        for ext in extensions:
            files.extend(p.glob(ext))
        files = sorted(set(files))

        if not files:
            print(f"[CLI] No audio files found in directory: {p}")
            return []

        out_dir = Path(output_dest).resolve() if output_dest else p
        out_dir.mkdir(parents=True, exist_ok=True)

        print(f"[CLI] Batch processing {len(files)} audio files in {p}...")
        for i, f_path in enumerate(files, 1):
            out_file = out_dir / f"{f_path.stem}_cleaned.wav"
            print(f"\n[{i}/{len(files)}] Processing: {f_path.name}")
            try:
                res = clean_file(f_path, out_file, **kwargs)
                results.append(res)
            except Exception as e:
                print(f"[ERROR] Failed processing {f_path.name}: {e}")
    else:
        raise FileNotFoundError(f"Target path does not exist: {p}")

    return results


def print_stats_table(results: List[Dict[str, Any]]):
    """Outputs structured summary statistics to stdout."""
    if not results:
        return
    print("\n" + "=" * 78)
    print(f"{'FILE':<24} {'DUR':<8} {'SPEED':<10} {'IN RMS':<11} {'OUT RMS':<11} {'ATTEN':<8}")
    print("-" * 78)
    for r in results:
        fname = Path(r["input_file"]).name
        if len(fname) > 22:
            fname = fname[:19] + "..."
        dur_str = f"{r['duration_sec']:.1f}s"
        spd_str = f"{r['speed_factor']:.1f}x"
        in_rms = f"{r['in_rms_db']:.1f} dB"
        out_rms = f"{r['out_rms_db']:.1f} dB"
        atten = f"{r['in_rms_db'] - r['out_rms_db']:.1f} dB"
        print(f"{fname:<24} {dur_str:<8} {spd_str:<10} {in_rms:<11} {out_rms:<11} {atten:<8}")
    print("=" * 78 + "\n")


def build_arg_parser() -> argparse.ArgumentParser:
    """Builds command-line parser for Getsu CLI."""
    parser = argparse.ArgumentParser(
        prog="getsu-cli",
        description="Getsu AI Noise Cancellation - High-Performance Headless Audio Cleaner",
    )
    parser.add_argument(
        "--clean",
        metavar="PATH",
        type=str,
        help="Input audio file or folder to clean",
    )
    parser.add_argument(
        "output_path",
        nargs="?",
        default=None,
        help="Destination audio file or directory (optional)",
    )
    parser.add_argument(
        "--no-rnnoise",
        action="store_true",
        help="Bypass RNNoise neural noise suppression (use gate/HPF only)",
    )
    parser.add_argument(
        "--no-gate",
        action="store_true",
        help="Bypass Adaptive Noise Gate (use RNNoise only)",
    )
    parser.add_argument(
        "--auto-level",
        action="store_true",
        help="Enable dynamic speech leveler normalizing voiced speech",
    )
    parser.add_argument(
        "--music-mode",
        action="store_true",
        help="Enable Music & Performance Mode (bypasses RNNoise, 800ms natural sustain)",
    )
    parser.add_argument(
        "--gain",
        type=float,
        default=0.0,
        help="Makeup gain boost in dB (default: 0.0 dB)",
    )
    parser.add_argument(
        "--stereo",
        action="store_true",
        help="Process multi-channel audio with coupled linked-stereo gating",
    )
    parser.add_argument(
        "--progress",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Display dynamic console progress bar",
    )
    parser.add_argument(
        "--stats",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Display audio processing statistics summary table",
    )
    parser.add_argument(
        "--report-json",
        metavar="FILE",
        type=str,
        default=None,
        help="Export per-frame telemetry JSON log",
    )
    parser.add_argument(
        "--check-devices",
        action="store_true",
        help="Print audio device diagnostics report and exit",
    )
    parser.add_argument(
        "--install-driver",
        action="store_true",
        help="Install bundled VB-Audio Virtual Cable driver and exit",
    )
    return parser


def main() -> int:
    """CLI cleaner entry point."""
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.check_devices:
        from src.devices import print_device_report
        print_device_report()
        return 0

    if args.install_driver:
        from src.devices import install_vbcable_driver
        return 0 if install_vbcable_driver() else 1

    if not args.clean:
        parser.print_help()
        return 1

    try:
        results = clean_batch(
            target_path=args.clean,
            output_dest=args.output_path,
            no_rnnoise=args.no_rnnoise,
            no_gate=args.no_gate,
            auto_level=args.auto_level,
            music_mode=args.music_mode,
            gain_db=args.gain,
            stereo=args.stereo,
            progress=args.progress,
            report_json=args.report_json,
        )
        if args.stats and results:
            print_stats_table(results)
        return 0
    except Exception as e:
        sys.stderr.write(f"\n[ERROR] Audio processing failed: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())

