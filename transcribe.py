#!/usr/bin/env python3
"""
transcribe.py — Record and transcribe a meeting to a Markdown file.

Records microphone and system audio simultaneously. During the meeting a
plain-text live transcript is written to the output file in real time.
When recording stops, a second pass merges both streams, deduplicates mic
echoes, and overwrites the file with a clean speaker-attributed transcript.

Usage:
    python3 transcribe.py meeting.md
    python3 transcribe.py --duration 60 meeting.md
    python3 transcribe.py --mic-only meeting.md
    python3 transcribe.py --devices
    python3 transcribe.py --setup
"""

import argparse
import datetime
import difflib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


# ── Paths ──────────────────────────────────────────────────────────────────

SCRIPT_DIR   = Path(__file__).resolve().parent
VENDOR_DIR   = SCRIPT_DIR / "vendor"

CACHE_DIR    = Path.home() / ".cache" / "meeting-transcriber"
WHISPER_DIR  = CACHE_DIR / "whisper.cpp"
MODEL_DIR    = CACHE_DIR / "models"
MODEL_PATH   = MODEL_DIR / "ggml-medium.en.bin"
MODEL_URL    = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.en.bin"

SWIFT_PROJECT  = VENDOR_DIR / "system-audio-tap"
SYSTEM_TAP_BIN = SWIFT_PROJECT / ".build" / "release" / "system-audio-tap"


# ── Configuration ──────────────────────────────────────────────────────────

DEFAULT_CHUNK = 15  # seconds


# ── Globals ────────────────────────────────────────────────────────────────

running        = True
whisper_bin    = None
pending_threads = []
file_lock      = threading.Lock()

# Accumulated segments for the final attribution pass:
# each entry is (absolute_datetime, "You"|"Them", text)
all_segments: list[tuple[datetime.datetime, str, str]] = []
segments_lock = threading.Lock()


# ── Logging ────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"\033[90m[{ts}]\033[0m {msg}", file=sys.stderr, flush=True)


# ── Setup ──────────────────────────────────────────────────────────────────

def ensure_ffmpeg():
    if shutil.which("ffmpeg"):
        return
    log("Installing ffmpeg via Homebrew...")
    subprocess.run(["brew", "install", "ffmpeg"], check=True)
    if not shutil.which("ffmpeg"):
        sys.exit("Error: ffmpeg installation failed")


def _find_whisper_bin():
    for name in ("whisper-cli", "main"):
        p = WHISPER_DIR / "build" / "bin" / name
        if p.exists() and os.access(p, os.X_OK):
            return p
    return None


def ensure_whisper():
    global whisper_bin
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if not (WHISPER_DIR / "CMakeLists.txt").exists():
        log("Cloning whisper.cpp...")
        subprocess.run(
            ["git", "clone", "--depth", "1",
             "https://github.com/ggerganov/whisper.cpp.git",
             str(WHISPER_DIR)],
            check=True,
        )

    whisper_bin = _find_whisper_bin()
    if not whisper_bin:
        log("Building whisper.cpp with Metal acceleration...")
        if not shutil.which("cmake"):
            log("Installing cmake via Homebrew...")
            subprocess.run(["brew", "install", "cmake"], check=True)
        build_dir = WHISPER_DIR / "build"
        subprocess.run(
            ["cmake", "-B", str(build_dir), "-DGGML_METAL=ON"],
            cwd=str(WHISPER_DIR),
            check=True,
        )
        subprocess.run(
            ["cmake", "--build", str(build_dir), "--config", "Release", "-j"],
            check=True,
        )
        whisper_bin = _find_whisper_bin()
        if not whisper_bin:
            sys.exit("Error: whisper.cpp build failed")
        log(f"Built: {whisper_bin}")

    if not MODEL_PATH.exists():
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        log("Downloading medium.en model (~1.5 GB)...")
        subprocess.run(
            ["curl", "-L", "--progress-bar", "-o", str(MODEL_PATH), MODEL_URL],
            check=True,
        )
        log("Model ready")


def ensure_system_audio_tap():
    if SYSTEM_TAP_BIN.exists() and os.access(SYSTEM_TAP_BIN, os.X_OK):
        return
    log("Building system-audio-tap (Swift, ScreenCaptureKit)...")
    subprocess.run(
        ["swift", "build", "-c", "release"],
        cwd=str(SWIFT_PROJECT),
        check=True,
    )
    if not SYSTEM_TAP_BIN.exists():
        sys.exit("Error: system-audio-tap build failed")
    log(f"Built: {SYSTEM_TAP_BIN}")


def setup():
    if not shutil.which("brew"):
        sys.exit("Error: Homebrew is required — https://brew.sh")
    ensure_ffmpeg()
    ensure_whisper()
    ensure_system_audio_tap()


# ── Audio devices ──────────────────────────────────────────────────────────

def list_audio_devices():
    r = subprocess.run(
        ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True,
        text=True,
    )
    in_audio = False
    devices = []
    for line in r.stderr.splitlines():
        if "AVFoundation audio devices:" in line:
            in_audio = True
            continue
        if in_audio:
            m = re.search(r"\[AVFoundation.*?\]\s+\[(\d+)\]\s+(.*)", line)
            if m:
                devices.append((int(m.group(1)), m.group(2).strip()))
    return devices


# ── Recording ──────────────────────────────────────────────────────────────

def record_mic_chunk(device_index, out_path, duration):
    cmd = [
        "ffmpeg", "-y",
        "-f", "avfoundation",
        "-i", f":{device_index}",
        "-ac", "1",
        "-ar", "16000",
        "-t", str(duration),
        "-loglevel", "error",
        str(out_path),
    ]
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL)


def record_system_chunk(out_path, duration):
    cmd = [
        str(SYSTEM_TAP_BIN),
        "--output", str(out_path),
        "--duration", str(duration),
    ]
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL)


# ── Transcription ──────────────────────────────────────────────────────────

def convert_to_whisper_format(in_path, out_path):
    subprocess.run(
        ["ffmpeg", "-y",
         "-i", str(in_path),
         "-ac", "1", "-ar", "16000",
         "-loglevel", "error",
         str(out_path)],
        check=True,
        timeout=30,
    )


def transcribe_timestamped(audio_path):
    """Run whisper and return [(start_sec, end_sec, text), ...]."""
    r = subprocess.run(
        [str(whisper_bin), "-m", str(MODEL_PATH), "-f", str(audio_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    segments = []
    for line in (r.stdout + "\n" + r.stderr).splitlines():
        m = re.match(
            r"\[(\d+):(\d+):(\d+\.\d+)\s*-->\s*(\d+):(\d+):(\d+\.\d+)\]\s*(.*)",
            line,
        )
        if m:
            s = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            e = int(m.group(4)) * 3600 + int(m.group(5)) * 60 + float(m.group(6))
            text = m.group(7).strip()
            if text and not re.match(r"^\[.*\]$", text) and not re.match(r"^\(.*\)$", text):
                segments.append((s, e, text))
    return segments


def transcribe_plain(audio_path):
    """Run whisper with no timestamps, return plain text."""
    r = subprocess.run(
        [str(whisper_bin), "-m", str(MODEL_PATH), "-f", str(audio_path), "-nt"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    text = r.stdout.strip() or r.stderr.strip()
    text = re.sub(r"\[BLANK_AUDIO\]", "", text)
    text = re.sub(r"\(silence\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(.{10,}?)\1{3,}", r"\1", text)
    return text.strip()


# ── Deduplication ──────────────────────────────────────────────────────────

def _text_similarity(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _is_contained_fragment(short_text, long_text, max_ratio=0.4):
    """Return True if short_text is a short fragment contained within long_text."""
    if not short_text or not long_text:
        return False
    if len(short_text) / len(long_text) > max_ratio:
        return False
    norm_short = re.sub(r"[^\w\s]", "", short_text.lower()).strip()
    norm_long  = re.sub(r"[^\w\s]", "", long_text.lower()).strip()
    return bool(norm_short) and norm_short in norm_long


def deduplicate(merged):
    """Remove You segments that are mic echoes of Them segments.

    Compares every You segment against all Them segments within a ±10 second
    window. Drops the You segment if text similarity exceeds 0.5 or if it is
    a short contained fragment of the Them segment (at most 40% of its length).
    """
    if not merged:
        return merged

    them_segs = [(dt, t) for dt, speaker, t in merged if speaker == "Them"]

    drop = set()
    for i, (dt, speaker, text) in enumerate(merged):
        if speaker != "You":
            continue
        for them_dt, them_text in them_segs:
            if abs((dt - them_dt).total_seconds()) > DEFAULT_CHUNK * 1.5:
                continue
            if _text_similarity(text, them_text) > 0.5 or \
               _is_contained_fragment(text, them_text):
                drop.add(i)
                break

    if drop:
        log(f"Dedup: dropped {len(drop)} mic echo(es)")

    return [seg for i, seg in enumerate(merged) if i not in drop]


# ── Live transcript (pass 1) ───────────────────────────────────────────────

def process_dual_chunk_live(mic_wav, sys_wav, md_path, chunk_start):
    """Transcribe both streams, write plain text live, accumulate for pass 2."""
    sys_converted = None
    try:
        sys_converted = sys_wav.with_suffix(".16k.wav")
        convert_to_whisper_format(sys_wav, sys_converted)

        mic_segs = transcribe_timestamped(mic_wav)
        sys_segs = transcribe_timestamped(sys_converted)

        # Accumulate for final pass
        with segments_lock:
            for s, _e, t in mic_segs:
                all_segments.append((
                    chunk_start + datetime.timedelta(seconds=s), "You", t
                ))
            for s, _e, t in sys_segs:
                all_segments.append((
                    chunk_start + datetime.timedelta(seconds=s), "Them", t
                ))

        # Write plain text to file (no attribution)
        all_text = " ".join(
            t for _, _, t in sorted(
                [(s, "You", t) for s, _e, t in mic_segs] +
                [(s, "Them", t) for s, _e, t in sys_segs],
                key=lambda x: x[0],
            )
        ).strip()

        if all_text:
            with file_lock:
                with open(md_path, "a") as f:
                    f.write(all_text + " ")
            log(f"{all_text[:80]}{'...' if len(all_text) > 80 else ''}")
        else:
            log("(no speech detected)")

    except Exception as exc:
        log(f"Transcription error: {exc}")
    finally:
        for p in (mic_wav, sys_wav, sys_converted):
            if p and p.exists():
                try:
                    os.unlink(p)
                except OSError:
                    pass


def process_single_chunk_live(mic_wav, md_path, chunk_start):
    """Transcribe mic only, write plain text live, accumulate for pass 2."""
    try:
        text = transcribe_plain(mic_wav)
        if not text:
            log("(no speech detected)")
            return

        with segments_lock:
            all_segments.append((chunk_start, "You", text))

        with file_lock:
            with open(md_path, "a") as f:
                f.write(text + " ")

        log(f"{text[:80]}{'...' if len(text) > 80 else ''}")

    except Exception as exc:
        log(f"Transcription error: {exc}")
    finally:
        try:
            os.unlink(mic_wav)
        except OSError:
            pass


# ── Final transcript (pass 2) ──────────────────────────────────────────────

def write_final_transcript(md_path, start_time):
    """Merge, deduplicate and write the attributed transcript, overwriting the file."""
    log("Writing final attributed transcript...")

    with segments_lock:
        merged = sorted(all_segments, key=lambda x: x[0])

    merged = deduplicate(merged)

    if not merged:
        log("No segments to write.")
        return

    lines = []
    for dt, speaker, text in merged:
        ts = dt.strftime("%H:%M:%S")
        lines.append(f"**[{ts}] {speaker}:** {text}")

    duration = datetime.datetime.now() - start_time
    minutes  = int(duration.total_seconds() // 60)
    seconds  = int(duration.total_seconds() % 60)

    with open(md_path, "w") as f:
        f.write("\n\n".join(lines) + "\n")

    log(f"Done. Duration: {minutes}m {seconds}s — {md_path}")


# ── Main loop ──────────────────────────────────────────────────────────────

def run(md_path, mic_idx, dual, chunk_duration, duration_minutes=None):
    global running

    tmp       = Path(tempfile.mkdtemp(prefix="transcribe_"))
    n         = 0
    start_time = datetime.datetime.now()

    deadline = None
    if duration_minutes:
        deadline = start_time + datetime.timedelta(minutes=duration_minutes)

    # Initialise file with a blank slate for live streaming
    with open(md_path, "w") as f:
        f.write("")

    mode    = "Dual (mic + system)" if dual else "Mic only"
    dur_str = f" | Duration: {duration_minutes}min" if duration_minutes else ""
    log(f"Output: {md_path}")
    log(f"Mode: {mode} | Chunk: {chunk_duration}s{dur_str}")
    if deadline:
        log(f"Will auto-stop at {deadline.strftime('%H:%M:%S')}")
    log("Recording — press Ctrl-C to stop\n")

    def on_signal(sig, _frame):
        global running
        running = False

    signal.signal(signal.SIGINT,  on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        while running:
            if deadline and datetime.datetime.now() >= deadline:
                log(f"Duration reached ({duration_minutes}min) — stopping.")
                break

            n += 1
            chunk_start = datetime.datetime.now()

            mic_wav  = tmp / f"mic_{n:05d}.wav"
            mic_proc = record_mic_chunk(mic_idx, mic_wav, chunk_duration)

            sys_proc = None
            sys_wav  = None
            if dual:
                sys_wav  = tmp / f"sys_{n:05d}.wav"
                sys_proc = record_system_chunk(sys_wav, chunk_duration)

            mic_proc.wait()
            if sys_proc:
                sys_proc.wait()

            if not mic_wav.exists() or mic_wav.stat().st_size < 1000:
                if not running:
                    break
                continue

            if dual and sys_wav and sys_wav.exists() and sys_wav.stat().st_size > 1000:
                t = threading.Thread(
                    target=process_dual_chunk_live,
                    args=(mic_wav, sys_wav, md_path, chunk_start),
                    daemon=True,
                )
            else:
                if sys_wav and sys_wav.exists():
                    try:
                        os.unlink(sys_wav)
                    except OSError:
                        pass
                t = threading.Thread(
                    target=process_single_chunk_live,
                    args=(mic_wav, md_path, chunk_start),
                    daemon=True,
                )
            t.start()
            pending_threads.append(t)
            pending_threads[:] = [t for t in pending_threads if t.is_alive()]

    finally:
        if pending_threads:
            log("Finishing pending transcriptions...")
            for t in pending_threads:
                t.join(timeout=60)
        shutil.rmtree(tmp, ignore_errors=True)
        write_final_transcript(md_path, start_time)


# ── CLI ────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Record and transcribe a meeting to Markdown.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s meeting.md                  Record mic + system audio
  %(prog)s --duration 60 meeting.md    Auto-stop after 60 minutes
  %(prog)s --mic-only notes.md         Mic only, no system audio
  %(prog)s --devices                   List available microphones
  %(prog)s --mic 2 meeting.md          Use a specific microphone
  %(prog)s --setup                     Install dependencies only
        """,
    )
    p.add_argument("file",      nargs="?", help="Markdown file to write transcript to")
    p.add_argument("--setup",   action="store_true", help="Install dependencies and exit")
    p.add_argument("--devices", action="store_true", help="List audio input devices")
    p.add_argument("--duration", type=float, default=None, metavar="MIN",
                   help="Auto-stop after this many minutes")
    p.add_argument("--mic",     type=int, default=None, metavar="IDX",
                   help="Microphone device index (default: auto-detect)")
    p.add_argument("--mic-only", action="store_true",
                   help="Record microphone only, skip system audio")
    p.add_argument("--chunk",   type=int, default=DEFAULT_CHUNK, metavar="SEC",
                   help=f"Chunk duration in seconds (default: {DEFAULT_CHUNK})")

    args = p.parse_args()

    setup()

    if args.setup:
        log("All dependencies ready.")
        return

    if args.devices:
        devs = list_audio_devices()
        if not devs:
            print("No audio devices found.")
        else:
            print("Microphone devices (use index with --mic):\n")
            for idx, name in devs:
                print(f"  [{idx}]  {name}")
            print("\n  System audio is captured automatically via ScreenCaptureKit.")
        return

    if not args.file:
        p.error("Please provide a markdown file path")

    md_path = Path(args.file).resolve()
    md_path.parent.mkdir(parents=True, exist_ok=True)

    devs = list_audio_devices()
    if not devs:
        sys.exit("Error: no audio devices found")

    mic_idx = args.mic
    if mic_idx is None:
        for idx, name in devs:
            if "macbook" in name.lower() and "mic" in name.lower():
                mic_idx = idx
                break
        if mic_idx is None:
            mic_idx = devs[0][0]

    mic_name = next((n for i, n in devs if i == mic_idx), f"Device {mic_idx}")
    log(f"Mic: [{mic_idx}] {mic_name}")

    dual = False
    if not args.mic_only:
        if SYSTEM_TAP_BIN.exists() and os.access(SYSTEM_TAP_BIN, os.X_OK):
            dual = True
            log("System audio: ScreenCaptureKit")
        else:
            log("System audio: not available (run --setup to build system-audio-tap)")
    else:
        log("System audio: disabled (--mic-only)")

    run(md_path, mic_idx, dual, args.chunk, args.duration)


if __name__ == "__main__":
    main()
