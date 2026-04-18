import json
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "meeting-transcriber" / "config.json"

DEFAULTS: dict = {
    "output_dir": "~/Documents/Transcripts",
    "input_only": False,
    "last_transcript": None,
    "launch_at_login": False,
    "venv_python": None,       # Absolute path set by --setup; used by .app to invoke transcribe.py
    "transcribe_script": None, # Absolute path set by --setup; points to transcribe.py in the repo
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
            return {**DEFAULTS, **data}
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULTS)


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
