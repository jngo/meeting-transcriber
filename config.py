import json
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "meeting-transcriber" / "config.json"

DEFAULTS: dict = {
    "output_dir": "~/Documents/Transcripts",
    "input_only": False,
    "last_transcript": None,
    "launch_at_login": False,
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
