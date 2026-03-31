---
name: transcribe-meeting
description: Start, stop, and recover a meeting transcription session. Use when the user wants to transcribe a meeting from Claude Code.
compatibility: Requires macOS 14+, Python 3.x, Homebrew, and Screen Recording permission for the terminal.
allowed-tools: Bash Read TaskStop
metadata:
  author: jngo
  version: "2.0"
---

# Transcribe Meeting

Transcribes input and system audio simultaneously using whisper.cpp and ScreenCaptureKit. Streams a plain-text live transcript to the output file during the meeting. When stopped, runs a final pass that deduplicates input echoes and overwrites the file with a clean speaker-attributed transcript (`**[HH:MM:SS] You:**` / `**[HH:MM:SS] Them:**`).

Because TaskStop cannot deliver a signal to child processes, the final pass does not run inside the transcription process itself. Instead, transcribed segments are persisted to a session file (`<output>.md.session`) throughout the transcription. After TaskStop, `--recover` reads the session file, runs the final pass, and deletes it. On a clean exit the session file is deleted automatically — the user is none the wiser.

## Credentials

Screen Recording permission must be granted to the terminal app in System Settings → Privacy & Security → Screen Recording.

## Setup

On first run, install dependencies:

```bash
transcribe-meeting --setup
```

## Steps

1. Ask the user for the meeting title or topic.
2. Suggest an output path: `00 Inbox/Transcripts/YYYY-MM-DD <title>.md` based on today's date.
3. Start transcription in the background:
   ```bash
   transcribe-meeting "<output-path>"
   ```
4. Note the task ID and confirm to the user that transcription has started.
5. When the user asks to stop, use TaskStop to stop the transcription.
6. Check whether a session file exists:
   ```bash
   test -f "<output-path>.session" && echo "exists" || echo "missing"
   ```
7. **If the session file exists:** the transcription was interrupted. Run recovery:
   ```bash
   transcribe-meeting --recover "<output-path>"
   ```
   This reads the session file, runs deduplication and speaker attribution, writes the final transcript, and deletes the session file. It completes in under a second. Inform the user with a single brief line: *"Transcript saved."*
8. **If the session file is missing:** the process exited cleanly and the final pass already ran. Proceed without mentioning recovery at all.
9. Read the output file and find the first and last `[HH:MM:SS]` timestamps. Report: **"Transcription complete. Duration: X min. Saved to: [path]"**
10. If no timestamps are found in the file, report that no speech was detected.

## Arguments

| Argument | Required | Default | Description |
|---|---|---|---|
| `file` | Yes | — | Markdown file to write transcript to |
| `--input-only` | No | Off | Transcribe input only, skip system audio |
| `--input <IDX>` | No | Auto | Audio input device index |
| `--chunk <SEC>` | No | 15 | Chunk duration in seconds |
| `--list-inputs` | No | — | List available audio input devices |
| `--recover` | No | — | Recover transcript from an interrupted transcription session |
| `--setup` | No | — | Install dependencies only |
