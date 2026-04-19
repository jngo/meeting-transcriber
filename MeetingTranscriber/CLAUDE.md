# Meeting Transcriber — Swift App

Native macOS menu bar app that transcribes meetings with speaker attribution ("You" / "Them").
Standalone repo; no dependency on the Python `meeting-transcriber` CLI.

See `plan.md` for full architecture and phased implementation plan.

---

## Current State

**Phase 1 (scaffold) is complete.** The app compiles and runs:
- Menu bar icon with correct menus for all four states
- IDLE → RECORDING → FINALIZING → DONE state machine with pulsing icon, braille spinner, elapsed timer
- Config persisted in Application Support (security-scoped bookmark for output directory)
- App Sandbox enabled; WhisperKit SPM dependency declared

Stop Recording completes immediately — no audio capture or transcription yet.

---

## Project Setup

```bash
brew install xcodegen
xcodegen generate          # creates MeetingTranscriber.xcodeproj
open MeetingTranscriber.xcodeproj
```

Xcode resolves the WhisperKit SPM package on first open (~500 MB model download happens at runtime, not build time).

---

## Key Decisions (do not relitigate without good reason)

- **WhisperKit** (not whisper-cli subprocess) — in-process CoreML inference, no bundled binary, no quarantine issues
- **App Sandbox enabled** — security-scoped bookmarks for output dir; enables distribution to colleagues
- **NSStatusBar + NSMenu directly** (not SwiftUI MenuBarExtra) — reliable `DispatchSourceTimer` animation, correct activation policy control for dialogs
- **DispatchSourceTimer** (not `Timer`) — fires in any run loop mode including `NSEventTrackingRunLoopMode`, so elapsed timer updates correctly while menu is open
- **`LSUIElement = true`** in Info.plist — no Dock icon; temporarily flip to `.regular` activation policy before showing dialogs (`showDialog` helper in `StatusBarController`)

---

## Reference Implementation

The Python `meeting-transcriber` repo is the spec for everything not yet built.
Key files to read before implementing each phase:

| Phase | Reference |
|---|---|
| 2 — Mic capture | `transcribe.py`: `run()`, ffmpeg invocation (replace with AVAudioEngine) |
| 3 — System audio | `vendor/system-audio-tap/Sources/main.swift`: port `AudioCaptureDelegate`, `AudioFileWriter`, `runCapture()` verbatim into `SystemAudioCapturer.swift` + `WAVWriter.swift` |
| 4 — Transcription | `transcribe.py`: `process_dual_chunk_live()`, whisper invocation/parsing, `_append_session()` |
| 5 — Output | `transcribe.py`: `write_final_transcript()`, `deduplicate()`, session JSONL format, Markdown format |
| 6 — UI polish | `gui.py`: dialog patterns, orphan recovery, launch-at-login |

---

## Architecture Notes

### Audio chunking (Phases 2–3)
`ChunkCoordinator` owns a `DispatchSourceTimer` (15s). On each tick it signals both `MicrophoneCapturer` (AVAudioEngine tap) and `SystemAudioCapturer` (SCK) to seal their current buffer and hand off WAV file paths. A `DispatchGroup` waits for both, then dispatches to `ChunkTranscriber`.

Mic output: 16 kHz mono Float32 WAV (whisper-preferred).  
System audio output: 48 kHz stereo Float32 WAV — WhisperKit resamples internally, no `AVAudioConverter` needed.

### Speaker attribution (Phase 5)
`Deduplicator.swift` ports Python's `deduplicate()`: LCS character ratio (`2 * lcs / (len(a) + len(b))`) ≈ `SequenceMatcher.ratio()`. Drop "You" segment if ratio > 0.5 with any "Them" within ±22.5 s, or if "You" is a short fragment (≤ 40% length) contained in "Them".

### Session file (Phase 4)
JSONL at `{outputDir}/{filename}.md.session`. One entry per line:
```json
{"dt": "2026-04-18T14:23:45.123456", "speaker": "You", "text": "transcribed text"}
```
Use `ISO8601DateFormatter` with `.withInternetDateTime | .withFractionalSeconds`.

### Markdown output (Phase 5)
```
**[HH:MM:SS] You:** segment text

**[HH:MM:SS] Them:** segment text
```
Write atomically (`Data.write(to:options:.atomic)`).

### ScreenCaptureKit permission (Phase 3)
Call `SCShareableContent.current` only on first Start Recording click — never at launch. Show `NSAlert` → System Settings if denied.

### Launch at login (Phase 6)
`SMAppService.mainApp.register()` — no launchd plist, no `launchctl`.
