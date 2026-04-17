# meeting-transcriber

Transcribes input and system audio simultaneously, producing a speaker-attributed Markdown transcript using [whisper.cpp](https://github.com/ggerganov/whisper.cpp) with Metal acceleration.

During a session, a plain-text live transcript is streamed to the output file. When you press Ctrl-C, a final pass merges both audio streams, deduplicates input echoes of system audio, and overwrites the file with a clean attributed transcript.

## Requirements

- macOS 14+ (Sonoma)
- Python 3
- [ffmpeg](https://ffmpeg.org)
- [whisper.cpp](https://github.com/ggerganov/whisper.cpp)
- Homebrew — used by `--setup` to install ffmpeg and build whisper.cpp
- Screen Recording permission granted to your terminal app

## Setup

```bash
python3 transcribe.py --setup
```

Installs ffmpeg, builds whisper.cpp, downloads the `medium.en` model (~~1.5 GB), builds the `system-audio-tap` Swift binary, and symlinks `transcribe-meeting` to `~~/.local/bin/`.

## Usage

```bash
transcribe-meeting meeting.md
transcribe-meeting --input-only notes.md
```

Press **Ctrl-C** to stop. The script finishes the current chunk, then writes the final attributed transcript.

By default, system audio is **required**. If ScreenCaptureKit capture fails, the script retries short recovery probes with an attempts/timeout budget and then exits with an error if recovery is not possible. Use `--input-only` only when microphone-only transcription is intentional.

## Options


| Flag            | Default | Description                                     |
| --------------- | ------- | ----------------------------------------------- |
| `--input-only`  | Off     | Transcribe input device only, skip system audio |
| `--input <IDX>` | Auto    | Audio input device index                        |
| `--chunk <SEC>` | 15      | Chunk duration in seconds                       |
| `--list-inputs` | —       | List available audio input devices              |
| `--recover`     | —       | Recover transcript from an interrupted session  |
| `--setup`       | —       | Install dependencies only                       |


## Output format

```
**[HH:MM:SS] You:** text from your microphone

**[HH:MM:SS] Them:** text from system audio
```

## Credits

Based on [Matej Hrescak’s](https://hrescak.com) ([@hrescak)](https://github.com/hrescak) [transcribe-md](https://github.com/hrescak/transcribe-md). The vendored `system-audio-tap` Swift binary is from that project. This fork adapts the transcription script for standalone terminal use with a two-pass architecture: live plain-text streaming during recording, followed by a deduplicated speaker-attributed transcript on exit.