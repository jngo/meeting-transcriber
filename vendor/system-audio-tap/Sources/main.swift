import AVFAudio
import Foundation
import ScreenCaptureKit

// ── Signal handling ─────────────────────────────────────────────────────────

private var shouldRun = true
private func onSignal(_: Int32) { shouldRun = false }
signal(SIGINT, onSignal)
signal(SIGTERM, onSignal)

// ── Argument parsing ────────────────────────────────────────────────────────

var outputPath = "output.wav"
var duration: TimeInterval = 0
var debugMode = false

var rawArgs = Array(CommandLine.arguments.dropFirst())
var argIdx = 0
while argIdx < rawArgs.count {
    switch rawArgs[argIdx] {
    case "--output", "-o":
        argIdx += 1
        guard argIdx < rawArgs.count else { fputs("Missing value for --output\n", stderr); exit(1) }
        outputPath = rawArgs[argIdx]
    case "--duration", "-d":
        argIdx += 1
        guard argIdx < rawArgs.count, let d = TimeInterval(rawArgs[argIdx]) else {
            fputs("Missing value for --duration\n", stderr); exit(1)
        }
        duration = d
    case "--debug":
        debugMode = true
    case "--help", "-h":
        print("""
        system-audio-tap — Capture system audio via ScreenCaptureKit (macOS 14+)

        Usage: system-audio-tap --output FILE [--duration SECONDS] [--debug]

        Options:
          -o, --output FILE      Output WAV file path (default: output.wav)
          -d, --duration SECS    Recording duration in seconds (0 = until Ctrl-C)
          --debug                Print detailed diagnostics
          -h, --help             Show this help
        """)
        exit(0)
    default:
        fputs("Unknown argument: \(rawArgs[argIdx])\n", stderr)
        exit(1)
    }
    argIdx += 1
}

func dbg(_ msg: String) {
    if debugMode { fputs("[debug] \(msg)\n", stderr) }
}

// ── Audio file writer ───────────────────────────────────────────────────────

final class AudioFileWriter {
    private var file: AVAudioFile?
    private(set) var framesWritten: Int64 = 0
    private var writeErrors: Int = 0

    init(url: URL, format: AVAudioFormat) throws {
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: format.sampleRate,
            AVNumberOfChannelsKey: format.channelCount,
        ]
        file = try AVAudioFile(
            forWriting: url,
            settings: settings,
            commonFormat: .pcmFormatFloat32,
            interleaved: format.isInterleaved
        )
    }

    func write(_ buffer: AVAudioPCMBuffer) {
        do {
            try file?.write(from: buffer)
            framesWritten += Int64(buffer.frameLength)
        } catch {
            writeErrors += 1
            if writeErrors <= 5 { fputs("Write error: \(error)\n", stderr) }
        }
    }

    func close() {
        if writeErrors > 0 {
            fputs("Stats: \(framesWritten) frames, \(writeErrors) write errors\n", stderr)
        }
        file = nil
    }
}

// ── SCStream delegate ───────────────────────────────────────────────────────

@available(macOS 14.0, *)
class AudioCaptureDelegate: NSObject, SCStreamOutput {
    let writer: AudioFileWriter
    let format: AVAudioFormat
    var callbackCount: Int64 = 0
    var nonSilentCallbacks: Int64 = 0

    init(writer: AudioFileWriter, format: AVAudioFormat) {
        self.writer = writer
        self.format = format
    }

    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of type: SCStreamOutputType
    ) {
        guard type == .audio else { return }
        callbackCount += 1

        guard let formatDesc = sampleBuffer.formatDescription,
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc)
        else {
            dbg("callback #\(callbackCount): no format description")
            return
        }

        // Create AVAudioFormat from the sample buffer's format
        guard let bufferFormat = AVAudioFormat(streamDescription: asbd) else {
            dbg("callback #\(callbackCount): cannot create AVAudioFormat")
            return
        }

        // Get the required buffer list size first (SCStream delivers non-interleaved
        // audio with one AudioBuffer per channel, so a bare AudioBufferList is too small)
        var requiredSize: Int = 0
        CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: &requiredSize,
            bufferListOut: nil,
            bufferListSize: 0,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: 0,
            blockBufferOut: nil
        )

        guard requiredSize > 0 else {
            dbg("callback #\(callbackCount): zero buffer list size")
            return
        }

        // Allocate properly-sized buffer list and extract audio
        let ablMemory = UnsafeMutablePointer<UInt8>.allocate(capacity: requiredSize)
        defer { ablMemory.deallocate() }
        let ablPtr = ablMemory.withMemoryRebound(to: AudioBufferList.self, capacity: 1) { $0 }

        var blockBuffer: CMBlockBuffer?
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: ablPtr,
            bufferListSize: requiredSize,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
            blockBufferOut: &blockBuffer
        )

        guard status == noErr else {
            dbg("callback #\(callbackCount): cannot get audio buffer list (\(status))")
            return
        }

        guard let pcmBuffer = AVAudioPCMBuffer(
            pcmFormat: bufferFormat,
            bufferListNoCopy: ablPtr,
            deallocator: nil
        ) else {
            dbg("callback #\(callbackCount): cannot create PCM buffer")
            return
        }

        // Debug: check for non-silence in first 20 callbacks
        if debugMode && callbackCount <= 20 {
            if let floatData = pcmBuffer.floatChannelData {
                var maxSample: Float = 0
                let frameCount = Int(pcmBuffer.frameLength)
                for i in 0..<min(frameCount, 512) {
                    let s = abs(floatData[0][i])
                    if s > maxSample { maxSample = s }
                }
                if callbackCount <= 10 {
                    dbg("callback #\(callbackCount): frames=\(frameCount) maxSample=\(maxSample)")
                }
                if maxSample > 0.001 { nonSilentCallbacks += 1 }
            }
        } else if callbackCount > 20 {
            // Still count non-silent for summary
            if let floatData = pcmBuffer.floatChannelData {
                var maxSample: Float = 0
                let frameCount = Int(pcmBuffer.frameLength)
                for i in 0..<min(frameCount, 128) {
                    let s = abs(floatData[0][i])
                    if s > maxSample { maxSample = s }
                }
                if maxSample > 0.001 { nonSilentCallbacks += 1 }
            }
        }

        // Convert format if needed, otherwise write directly
        if bufferFormat == format {
            writer.write(pcmBuffer)
        } else if let converter = AVAudioConverter(from: bufferFormat, to: format) {
            let frameCapacity = AVAudioFrameCount(
                Double(pcmBuffer.frameLength) * format.sampleRate / bufferFormat.sampleRate
            )
            guard let convertedBuffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameCapacity) else {
                return
            }
            var error: NSError?
            var done = false
            converter.convert(to: convertedBuffer, error: &error) { _, outStatus in
                if done {
                    outStatus.pointee = .endOfStream
                    return nil
                }
                done = true
                outStatus.pointee = .haveData
                return pcmBuffer
            }
            if error == nil && convertedBuffer.frameLength > 0 {
                writer.write(convertedBuffer)
            }
        } else {
            writer.write(pcmBuffer)
        }
    }
}

// ── Capture via ScreenCaptureKit ────────────────────────────────────────────

@available(macOS 14.0, *)
func runCapture() async {
    // 1. Get shareable content
    let content: SCShareableContent
    do {
        content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
    } catch {
        fputs("Error: cannot get shareable content: \(error)\n", stderr)
        fputs("Grant your terminal \"Screen Recording\" permission in System Settings > Privacy & Security.\n", stderr)
        exit(1)
    }

    guard let display = content.displays.first else {
        fputs("Error: no displays found\n", stderr)
        exit(1)
    }

    dbg("Display: \(display.width)x\(display.height)")
    dbg("Applications: \(content.applications.count)")
    dbg("Windows: \(content.windows.count)")

    // 2. Configure stream for audio-only capture
    let config = SCStreamConfiguration()
    config.capturesAudio = true
    config.excludesCurrentProcessAudio = true
    config.sampleRate = 48000
    config.channelCount = 2

    // Minimize video capture overhead (we only want audio)
    config.width = 2
    config.height = 2
    config.minimumFrameInterval = CMTime(value: 1, timescale: 1)  // 1 fps

    // 3. Create a content filter that captures everything (all apps)
    let filter = SCContentFilter(display: display, excludingWindows: [])

    // 4. Create the stream
    let stream: SCStream
    do {
        stream = SCStream(filter: filter, configuration: config, delegate: nil)
    }

    // 5. Set up audio output format
    let outputFormat = AVAudioFormat(
        commonFormat: .pcmFormatFloat32,
        sampleRate: 48000,
        channels: 2,
        interleaved: true
    )!

    // 6. Open output file
    let writer: AudioFileWriter
    do {
        writer = try AudioFileWriter(url: URL(fileURLWithPath: outputPath), format: outputFormat)
    } catch {
        fputs("Error: cannot open output file: \(error)\n", stderr)
        exit(1)
    }

    // 7. Set up delegate
    let delegate = AudioCaptureDelegate(writer: writer, format: outputFormat)
    let audioQueue = DispatchQueue(label: "audio-capture", qos: .userInteractive)

    do {
        try stream.addStreamOutput(delegate, type: .audio, sampleHandlerQueue: audioQueue)
    } catch {
        fputs("Error: cannot add audio output: \(error)\n", stderr)
        exit(1)
    }

    // 8. Start capturing
    do {
        try await stream.startCapture()
    } catch {
        fputs("Error: cannot start capture: \(error)\n", stderr)
        fputs("Grant your terminal \"Screen Recording\" permission in System Settings > Privacy & Security.\n", stderr)
        exit(1)
    }

    let durationNote = duration > 0 ? " (\(Int(duration))s)" : ""
    fputs("Recording system audio via ScreenCaptureKit -> \(outputPath)\(durationNote)\n", stderr)

    // 9. Wait
    if duration > 0 {
        let deadline = Date().addingTimeInterval(duration)
        while shouldRun && Date() < deadline {
            try? await Task.sleep(nanoseconds: 50_000_000)  // 50ms
        }
    } else {
        while shouldRun {
            try? await Task.sleep(nanoseconds: 100_000_000)  // 100ms
        }
    }

    // 10. Cleanup
    fputs("\nStopping...\n", stderr)
    do {
        try await stream.stopCapture()
    } catch {
        dbg("Stop error (non-fatal): \(error)")
    }

    writer.close()

    fputs("Frames written: \(writer.framesWritten)\n", stderr)
    if debugMode {
        fputs("[debug] \(delegate.callbackCount) callbacks, \(delegate.nonSilentCallbacks) non-silent\n", stderr)
    }
    fputs("Saved: \(outputPath)\n", stderr)
}

// ── Entry point ─────────────────────────────────────────────────────────────

if #available(macOS 14.0, *) {
    // Use a semaphore to bridge async to sync for the top-level
    let semaphore = DispatchSemaphore(value: 0)
    Task {
        await runCapture()
        semaphore.signal()
    }
    // Keep the main run loop alive for signal handling and async work
    while shouldRun {
        RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        if semaphore.wait(timeout: .now()) == .success {
            break
        }
    }
    if !shouldRun {
        // Signal received — give the async task a moment to clean up
        Thread.sleep(forTimeInterval: 0.5)
    }
} else {
    fputs("Error: macOS 14.0+ is required for ScreenCaptureKit audio capture.\n", stderr)
    exit(1)
}
