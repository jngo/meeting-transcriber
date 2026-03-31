# meeting-transcriber

Records microphone and system audio simultaneously and produces a speaker-attributed Markdown transcript using [whisper.cpp](https://github.com/ggerganov/whisper.cpp) with Metal acceleration.

During recording, a plain-text live transcript is streamed to the output file. When you press Ctrl-C, a final pass merges both audio streams, deduplicates mic echoes of system audio, and overwrites the file with a clean attributed transcript.

## Requirements

- macOS 14+ (Sonoma)
- Python 3
- Homebrew
- Screen Recording permission granted to your terminal app

## Setup

```bash
python3 transcribe.py --setup
```

Installs ffmpeg, builds whisper.cpp, downloads the `medium.en` model (~1.5 GB), builds the `system-audio-tap` Swift binary, and symlinks `transcribe-meeting` to `~/.local/bin/`.

## Usage

```bash
transcribe-meeting meeting.md
transcribe-meeting --mic-only notes.md
```

Press **Ctrl-C** to stop. The script finishes the current chunk, then writes the final attributed transcript.

## Options

| Flag | Default | Description |
|---|---|---|
| `--mic-only` | Off | Record microphone only, skip system audio |
| `--mic <IDX>` | Auto | Microphone device index |
| `--chunk <SEC>` | 15 | Chunk duration in seconds |
| `--devices` | — | List available microphones |
| `--setup` | — | Install dependencies only |

## Output format

```
**[HH:MM:SS] You:** text from your microphone

**[HH:MM:SS] Them:** text from system audio
```

## Credits

Based on [transcribe-md](https://github.com/hrescak/transcribe-md) by [@hrescak](https://github.com/hrescak). The vendored `system-audio-tap` Swift binary is from that project. This fork adapts the transcription script for standalone terminal use with a two-pass architecture: live plain-text streaming during recording, followed by a deduplicated speaker-attributed transcript on exit.
