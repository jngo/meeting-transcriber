#!/usr/bin/env python3
import enum
import subprocess
import sys
from pathlib import Path

import rumps

from config import load_config, save_config
from runner import TRANSCRIBE_PY, TranscriptionRunner


class State(enum.Enum):
    IDLE = "idle"
    RECORDING = "recording"
    FINALIZING = "finalizing"
    DONE = "done"


SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

LAUNCH_AGENT_LABEL = "com.meeting-transcriber"
LAUNCH_AGENT_PATH = (
    Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
)
PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>{script}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
</dict>
</plist>
"""


class MeetingTranscriberApp(rumps.App):
    def __init__(self):
        super().__init__("⏺", quit_button=None)
        self._state = State.IDLE
        self._runner = TranscriptionRunner()
        self._cfg = load_config()
        self._spinner_idx = 0
        self._pulse_on = True

        # Reused menu item so in-place title updates work
        self._elapsed_item = rumps.MenuItem("Recording 0:00:00")

        # All timers start stopped; started/stopped on state transitions
        self._pulse_timer = rumps.Timer(self._tick_pulse, 1.0)
        self._elapsed_timer = rumps.Timer(self._tick_elapsed, 1.0)
        self._spinner_timer = rumps.Timer(self._tick_spinner, 0.1)
        self._done_timer = rumps.Timer(self._check_done, 0.5)
        self._restore_timer = rumps.Timer(self._restore_idle, 3.0)

        self._build_idle_menu()
        self._check_orphan_sessions()

    # ── Menu helpers ──────────────────────────────────────────────────────────

    def _set_menu(self, items):
        self.menu.clear()
        for item in items:
            if item is None:
                self.menu.add(rumps.separator)
            else:
                self.menu.add(item)

    def _build_idle_menu(self):
        last = self._cfg.get("last_transcript")
        open_item = rumps.MenuItem(
            "Open Last Transcript",
            callback=self._open_last if last else None,
        )
        login_label = (
            "✓ Launch at Login"
            if self._cfg.get("launch_at_login")
            else "Launch at Login"
        )
        self._set_menu([
            rumps.MenuItem("Start Recording", callback=self._start_recording),
            None,
            open_item,
            None,
            rumps.MenuItem(login_label, callback=self._toggle_launch_at_login),
            rumps.MenuItem("Settings…", callback=self._open_settings),
            None,
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ])

    # ── State transitions ─────────────────────────────────────────────────────

    def _enter_idle(self):
        self._state = State.IDLE
        self.title = "⏺"
        self._build_idle_menu()

    def _enter_recording(self):
        self._state = State.RECORDING
        self._pulse_on = True
        self.title = "●"
        self._elapsed_item.title = f"Recording {self._runner.elapsed()}"
        self._set_menu([
            self._elapsed_item,
            None,
            rumps.MenuItem("View Live Transcript", callback=self._view_live_transcript),
            None,
            rumps.MenuItem("Set Title…", callback=self._set_title),
            rumps.MenuItem("Set Save Location…", callback=self._set_save_location),
            None,
            rumps.MenuItem("Stop Recording", callback=self._stop_recording),
        ])
        self._pulse_timer.start()
        self._elapsed_timer.start()
        self._done_timer.start()

    def _enter_finalizing(self):
        self._state = State.FINALIZING
        self._pulse_timer.stop()
        self._elapsed_timer.stop()
        self._spinner_idx = 0
        self.title = SPINNER_FRAMES[0]
        self._set_menu([rumps.MenuItem("Finalizing transcript…")])
        self._spinner_timer.start()

    def _enter_done(self, final_path: Path | None):
        self._state = State.DONE
        self._spinner_timer.stop()
        self._cfg["last_transcript"] = str(final_path) if final_path else None
        save_config(self._cfg)
        # Build idle menu immediately so Start Recording is available for back-to-back
        self._build_idle_menu()
        self._state = State.DONE  # _build_idle_menu doesn't touch _state, but be explicit
        self.title = "✓"
        if final_path:
            rumps.notification(
                "Transcript saved",
                self._runner.elapsed(),
                final_path.stem,
                data=str(final_path),
            )
        self._restore_timer.start()

    def _restore_idle(self, _):
        self._restore_timer.stop()
        if self._state == State.DONE:
            self._state = State.IDLE
            self.title = "⏺"

    # ── Timer callbacks ───────────────────────────────────────────────────────

    def _tick_pulse(self, _):
        self._pulse_on = not self._pulse_on
        self.title = "●" if self._pulse_on else "○"

    def _tick_elapsed(self, _):
        self._elapsed_item.title = f"Recording {self._runner.elapsed()}"

    def _tick_spinner(self, _):
        self.title = SPINNER_FRAMES[self._spinner_idx % len(SPINNER_FRAMES)]
        self._spinner_idx += 1

    def _check_done(self, _):
        if not self._runner.done_event.is_set():
            return
        self._done_timer.stop()
        if self._state == State.RECORDING:
            # Process exited before user clicked Stop
            self._pulse_timer.stop()
            self._elapsed_timer.stop()
            self._enter_idle()
            rumps.notification(
                "Recording stopped",
                "",
                "The transcription process ended unexpectedly.",
            )
        elif self._state == State.FINALIZING:
            final_path = self._runner.finalize_path()
            self._enter_done(final_path)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _start_recording(self, _=None):
        if self._state not in (State.IDLE, State.DONE):
            return
        if self._state == State.DONE:
            self._restore_timer.stop()
        out_dir = Path(
            self._cfg.get("output_dir", "~/00 Inbox/Transcripts")
        ).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        self._runner.start(out_dir)
        self._enter_recording()

    def _stop_recording(self, _=None):
        if self._state != State.RECORDING:
            return
        self._runner.stop()
        self._enter_finalizing()

    def _set_title(self, _=None):
        w = rumps.Window(
            message="Enter a title for this recording:",
            title="Set Meeting Title",
            default_text=self._runner.queued_title or "",
            ok="Set",
            cancel="Cancel",
            dimensions=(300, 24),
        )
        resp = w.run()
        if resp.clicked and resp.text.strip():
            self._runner.set_title(resp.text.strip())

    def _set_save_location(self, _=None):
        result = subprocess.run(
            [
                "osascript", "-e",
                'POSIX path of (choose folder with prompt "Select output directory:")',
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            chosen = result.stdout.strip().rstrip("/")
            if chosen:
                self._runner.set_dir(Path(chosen))
                self._cfg["output_dir"] = chosen
                save_config(self._cfg)

    def _view_live_transcript(self, _=None):
        path = self._runner.md_path
        if path and path.exists():
            subprocess.run(["open", str(path)])

    def _open_last(self, _=None):
        last = self._cfg.get("last_transcript")
        if last and Path(last).exists():
            subprocess.run(["open", last])

    def _open_settings(self, _=None):
        w = rumps.Window(
            message="Default save directory for transcripts:",
            title="Settings",
            default_text=self._cfg.get("output_dir", "~/00 Inbox/Transcripts"),
            ok="Save",
            cancel="Cancel",
            dimensions=(400, 24),
        )
        resp = w.run()
        if resp.clicked and resp.text.strip():
            self._cfg["output_dir"] = resp.text.strip()
            save_config(self._cfg)

    def _toggle_launch_at_login(self, _=None):
        if self._cfg.get("launch_at_login"):
            subprocess.run(
                ["launchctl", "unload", str(LAUNCH_AGENT_PATH)],
                capture_output=True,
            )
            LAUNCH_AGENT_PATH.unlink(missing_ok=True)
            self._cfg["launch_at_login"] = False
        else:
            plist = PLIST_TEMPLATE.format(
                label=LAUNCH_AGENT_LABEL,
                python=sys.executable,
                script=str(Path(__file__).resolve()),
            )
            LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
            LAUNCH_AGENT_PATH.write_text(plist)
            subprocess.run(
                ["launchctl", "load", str(LAUNCH_AGENT_PATH)],
                capture_output=True,
            )
            self._cfg["launch_at_login"] = True
        save_config(self._cfg)
        self._build_idle_menu()  # Update checkmark

    # ── Crash recovery ────────────────────────────────────────────────────────

    def _check_orphan_sessions(self):
        out_dir = Path(
            self._cfg.get("output_dir", "~/00 Inbox/Transcripts")
        ).expanduser()
        if not out_dir.exists():
            return
        orphans = sorted(out_dir.glob("*.md.session"))
        if not orphans:
            return
        names = "\n".join(f"  • {p.name[:-8]}" for p in orphans[:3])
        if len(orphans) > 3:
            names += f"\n  … and {len(orphans) - 3} more"
        response = rumps.alert(
            title="Incomplete recordings found",
            message=f"These recordings were interrupted:\n\n{names}\n\nRecover them now?",
            ok="Recover",
            cancel="Dismiss",
        )
        if response == 1:
            for orphan in orphans:
                md_path = orphan.parent / orphan.name[:-8]
                subprocess.Popen(
                    [sys.executable, str(TRANSCRIBE_PY), "--recover", str(md_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

    # ── Notification handler ──────────────────────────────────────────────────

    @rumps.notifications
    def notification_center(self, info):
        if isinstance(info, str) and Path(info).exists():
            subprocess.run(["open", info])


if __name__ == "__main__":
    MeetingTranscriberApp().run()
