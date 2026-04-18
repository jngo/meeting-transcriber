import datetime
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from config import load_config


def resolve_transcribe_cmd() -> tuple[str, str]:
    """Return (python_path, script_path) for invoking transcribe.py.

    Paths are stored in config by --setup so they work when the app is launched
    from Finder (where sys.executable is the bundled Python and the repo is not
    on sys.path).
    """
    cfg = load_config()
    python = cfg.get("venv_python") or sys.executable
    script = cfg.get("transcribe_script") or str(
        Path(__file__).resolve().parent / "transcribe.py"
    )
    return str(python), str(script)


def _subprocess_env() -> dict:
    """Return environ with Homebrew and user bin paths prepended.

    Apps launched from Finder inherit a minimal PATH (/usr/bin:/bin only),
    so ffmpeg and whisper-cli won't be found without this.
    """
    env = os.environ.copy()
    prepend = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        str(Path.home() / ".local" / "bin"),
    ]
    current = env.get("PATH", "")
    extra = ":".join(p for p in prepend if p not in current)
    env["PATH"] = f"{extra}:{current}" if current else extra
    return env


class TranscriptionRunner:
    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._md_path: Path | None = None
        self._date_prefix: str = ""
        self._start_time: datetime.datetime | None = None
        self._final_elapsed: str | None = None
        self._queued_title: str | None = None
        self._queued_dir: Path | None = None
        self.done_event = threading.Event()

    def start(self, out_dir: Path, on_done=None):
        now = datetime.datetime.now()
        self._date_prefix = now.strftime("%Y-%m-%d %H-%M")
        self._md_path = out_dir / f"{self._date_prefix} Untitled.md"
        self._start_time = now
        self._final_elapsed = None
        self._queued_title = None
        self._queued_dir = None
        self.done_event.clear()
        self._md_path.touch()  # Ensure file exists before subprocess initialises
        python, script = resolve_transcribe_cmd()
        self._proc = subprocess.Popen(
            [python, script, str(self._md_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_subprocess_env(),
        )
        threading.Thread(target=self._monitor, args=(on_done,), daemon=True).start()

    def stop(self):
        if self._proc and self._proc.poll() is None:
            self._final_elapsed = self.elapsed()
            self._proc.send_signal(signal.SIGINT)

    def set_title(self, title: str):
        self._queued_title = title.strip() or None

    def set_dir(self, dir_path: Path):
        self._queued_dir = dir_path

    @property
    def queued_title(self) -> str | None:
        return self._queued_title

    @property
    def md_path(self) -> Path | None:
        return self._md_path

    def elapsed(self) -> str:
        if self._final_elapsed is not None:
            return self._final_elapsed
        if self._start_time is None:
            return "0:00:00"
        total = int((datetime.datetime.now() - self._start_time).total_seconds())
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}"

    def finalize_path(self) -> Path | None:
        """Move/rename the output file after the subprocess exits. Returns the final path."""
        if self._md_path is None:
            return None
        src = self._md_path
        out_dir = (
            Path(self._queued_dir).expanduser()
            if self._queued_dir
            else src.parent
        )
        name = (
            f"{self._date_prefix} {self._queued_title}.md"
            if self._queued_title
            else src.name
        )
        dest = out_dir / name
        if src != dest:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.exists():
                src.rename(dest)
            src_session = Path(str(src) + ".session")
            if src_session.exists():
                src_session.rename(Path(str(dest) + ".session"))
        return dest

    def _monitor(self, on_done):
        self._proc.wait()
        self.done_event.set()
        if on_done:
            on_done()
