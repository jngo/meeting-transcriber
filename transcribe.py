#!/usr/bin/env python3
"""
transcribe.py — Record and transcribe a meeting to a Markdown file.

Records input and system audio simultaneously. During the meeting a
plain-text live transcript is written to the output file in real time.
When recording stops, a second pass merges both streams, deduplicates input
echoes, and overwrites the file with a clean speaker-attributed transcript.

Usage:
    python3 transcribe.py meeting.md
    python3 transcribe.py --input-only meeting.md
    python3 transcribe.py --list-inputs
    python3 transcribe.py --setup
"""

import argparse
import datetime
import difflib
import json
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
session_lock   = threading.Lock()

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


def ensure_command():
    """Make the script executable and symlink it as transcribe-meeting on PATH."""
    script = Path(__file__).resolve()
    script.chmod(script.stat().st_mode | 0o111)

    bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    link = bin_dir / "transcribe-meeting"
    if not link.exists() or link.resolve() != script:
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(script)
        log(f"Linked: transcribe-meeting → {script}")


def setup():
    if not shutil.which("brew"):
        sys.exit("Error: Homebrew is required — https://brew.sh")
    ensure_ffmpeg()
    ensure_whisper()
    ensure_system_audio_tap()
    ensure_command()


# ── Audio inputs ───────────────────────────────────────────────────────────

def list_audio_inputs():
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

def record_input_chunk(device_index, out_path, duration):
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
    """Remove You segments that are input echoes of Them segments.

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
        log(f"Dedup: dropped {len(drop)} input echo(es)")

    return [seg for i, seg in enumerate(merged) if i not in drop]


# ── Session file ───────────────────────────────────────────────────────────

def _session_path(md_path):
    return md_path.parent / (md_path.name + ".session")


def _append_session(md_path, dt, speaker, text):
    """Append a transcribed segment to the session file as a JSONL entry.

    The session file persists all segments to disk throughout recording so they
    survive a non-clean exit. On a normal Ctrl-C exit it is deleted silently by
    run(). If the process is killed before that (e.g. via TaskStop from Claude
    Code), run --recover to produce the final attributed transcript from it.
    """
    entry = json.dumps({"dt": dt.isoformat(), "speaker": speaker, "text": text})
    with session_lock:
        with open(_session_path(md_path), "a") as f:
            f.write(entry + "\n")


# ── Live transcript (pass 1) ───────────────────────────────────────────────

def process_dual_chunk_live(input_wav, sys_wav, md_path, chunk_start):
    """Transcribe both streams, write plain text live, accumulate for pass 2."""
    sys_converted = None
    try:
        sys_converted = sys_wav.with_suffix(".16k.wav")
        convert_to_whisper_format(sys_wav, sys_converted)

        input_segs = transcribe_timestamped(input_wav)
        sys_segs   = transcribe_timestamped(sys_converted)

        # Accumulate for final pass and persist to session file
        with segments_lock:
            for s, _e, t in input_segs:
                dt = chunk_start + datetime.timedelta(seconds=s)
                all_segments.append((dt, "You", t))
                _append_session(md_path, dt, "You", t)
            for s, _e, t in sys_segs:
                dt = chunk_start + datetime.timedelta(seconds=s)
                all_segments.append((dt, "Them", t))
                _append_session(md_path, dt, "Them", t)

        # Write plain text to file (no attribution)
        all_text = " ".join(
            t for _, _, t in sorted(
                [(s, "You", t) for s, _e, t in input_segs] +
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
        for p in (input_wav, sys_wav, sys_converted):
            if p and p.exists():
                try:
                    os.unlink(p)
                except OSError:
                    pass


def process_single_chunk_live(input_wav, md_path, chunk_start):
    """Transcribe input only, write plain text live, accumulate for pass 2."""
    try:
        text = transcribe_plain(input_wav)
        if not text:
            log("(no speech detected)")
            return

        with segments_lock:
            all_segments.append((chunk_start, "You", text))
        _append_session(md_path, chunk_start, "You", text)

        with file_lock:
            with open(md_path, "a") as f:
                f.write(text + " ")

        log(f"{text[:80]}{'...' if len(text) > 80 else ''}")

    except Exception as exc:
        log(f"Transcription error: {exc}")
    finally:
        try:
            os.unlink(input_wav)
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

def run(md_path, input_idx, dual, chunk_duration):
    global running

    tmp        = Path(tempfile.mkdtemp(prefix="transcribe_"))
    n          = 0
    start_time = datetime.datetime.now()

    # Initialise file with a blank slate for live streaming
    with open(md_path, "w") as f:
        f.write("")

    mode = "Input + system audio" if dual else "Input only"
    log(f"Output: {md_path}")
    log(f"Mode: {mode} | Chunk: {chunk_duration}s")
    log("Recording — press Ctrl-C to stop\n")

    def on_signal(sig, _frame):
        global running
        running = False

    signal.signal(signal.SIGINT,  on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        while running:

            n += 1
            chunk_start = datetime.datetime.now()

            input_wav  = tmp / f"input_{n:05d}.wav"
            input_proc = record_input_chunk(input_idx, input_wav, chunk_duration)

            sys_proc = None
            sys_wav  = None
            if dual:
                sys_wav  = tmp / f"sys_{n:05d}.wav"
                sys_proc = record_system_chunk(sys_wav, chunk_duration)

            input_proc.wait()
            if sys_proc:
                sys_proc.wait()

            if not input_wav.exists() or input_wav.stat().st_size < 1000:
                if not running:
                    break
                continue

            if dual and sys_wav and sys_wav.exists() and sys_wav.stat().st_size > 1000:
                t = threading.Thread(
                    target=process_dual_chunk_live,
                    args=(input_wav, sys_wav, md_path, chunk_start),
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
                    args=(input_wav, md_path, chunk_start),
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
        session = _session_path(md_path)
        if session.exists():
            session.unlink()


# ── Recovery ───────────────────────────────────────────────────────────────

def recover(md_path):
    """Produce the final attributed transcript from a session file.

    Used when the recording was interrupted before the final pass could run —
    for example, when stopped via TaskStop from Claude Code. The session file
    is deleted on success.
    """
    session = _session_path(md_path)
    if not session.exists():
        sys.exit(f"No session file found at {session}\nNothing to recover.")

    segments = []
    with open(session) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                segments.append((
                    datetime.datetime.fromisoformat(entry["dt"]),
                    entry["speaker"],
                    entry["text"],
                ))
            except (json.JSONDecodeError, KeyError):
                continue

    if not segments:
        log("Session file is empty — nothing to recover.")
        session.unlink()
        return

    log(f"Recovering {len(segments)} segment(s)...")
    global all_segments
    with segments_lock:
        all_segments = segments

    start_time = segments[0][0]
    write_final_transcript(md_path, start_time)
    session.unlink()


# ── CLI ────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Record and transcribe a meeting to Markdown.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s meeting.md                Record input + system audio
  %(prog)s --input-only notes.md     Input device only, no system audio
  %(prog)s --list-inputs             List available input devices
  %(prog)s --input 2 meeting.md      Use a specific input device
  %(prog)s --setup                   Install dependencies only
  %(prog)s --recover meeting.md      Recover transcript from an interrupted recording
        """,
    )
    p.add_argument("file",           nargs="?", help="Markdown file to write transcript to")
    p.add_argument("--setup",        action="store_true", help="Install dependencies and exit")
    p.add_argument("--list-inputs",  action="store_true", help="List available audio input devices")
    p.add_argument("--recover",      action="store_true",
                   help="Recover transcript from an interrupted recording session")
    p.add_argument("--input",        type=int, default=None, metavar="IDX",
                   help="Audio input device index (default: auto-detect)")
    p.add_argument("--input-only",   action="store_true",
                   help="Record input device only, skip system audio")
    p.add_argument("--chunk",        type=int, default=DEFAULT_CHUNK, metavar="SEC",
                   help=f"Chunk duration in seconds (default: {DEFAULT_CHUNK})")

    args = p.parse_args()

    if args.recover:
        if not args.file:
            p.error("--recover requires a file path")
        recover(Path(args.file).resolve())
        return

    setup()

    if args.setup:
        log("All dependencies ready.")
        return

    if args.list_inputs:
        devs = list_audio_inputs()
        if not devs:
            print("No audio input devices found.")
        else:
            print("Audio input devices (use index with --input):\n")
            for idx, name in devs:
                print(f"  [{idx}]  {name}")
            print("\n  System audio is captured automatically via ScreenCaptureKit.")
        return

    if not args.file:
        p.error("Please provide a markdown file path")

    md_path = Path(args.file).resolve()
    md_path.parent.mkdir(parents=True, exist_ok=True)

    devs = list_audio_inputs()
    if not devs:
        sys.exit("Error: no audio input devices found")

    input_idx = args.input
    if input_idx is None:
        for idx, name in devs:
            if "macbook" in name.lower() and "mic" in name.lower():
                input_idx = idx
                break
        if input_idx is None:
            input_idx = devs[0][0]

    input_name = next((n for i, n in devs if i == input_idx), f"Device {input_idx}")
    log(f"Input: [{input_idx}] {input_name}")

    dual = False
    if not args.input_only:
        if SYSTEM_TAP_BIN.exists() and os.access(SYSTEM_TAP_BIN, os.X_OK):
            dual = True
            log("System audio: ScreenCaptureKit")
        else:
            log("System audio: not available (run --setup to build system-audio-tap)")
    else:
        log("System audio: disabled (--input-only)")

    run(md_path, input_idx, dual, args.chunk)


if __name__ == "__main__":
    main()
