import AppKit

private let spinnerFrames = Array("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")

final class StatusBarController: NSObject, NSMenuDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let session = RecordingSession()
    private(set) var config = AppConfig.load()

    private var pulseTimer: DispatchSourceTimer?
    private var spinnerTimer: DispatchSourceTimer?
    private var restoreTimer: DispatchSourceTimer?
    private var pulseOn = true
    private var spinnerIdx = 0

    private let elapsedItem: NSMenuItem = {
        let item = NSMenuItem(title: "Recording 0:00:00", action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }()

    override init() {
        super.init()
        statusItem.button?.title = "⏺"
        session.onStateChange = { [weak self] state in self?.handleStateChange(state) }
        applyMenu(for: session.state)
    }

    // ── State handling ─────────────────────────────────────────────────────────

    private func handleStateChange(_ state: RecordingState) {
        stopAllTimers()
        switch state {
        case .idle:
            statusItem.button?.title = "⏺"
            applyMenu(for: state)
        case .recording:
            pulseOn = true
            statusItem.button?.title = "●"
            elapsedItem.title = "Recording \(session.elapsed)"
            applyMenu(for: state)
            startPulseTimer()
        case .finalizing:
            spinnerIdx = 0
            statusItem.button?.title = String(spinnerFrames[0])
            applyMenu(for: state)
            startSpinnerTimer()
        case .done:
            statusItem.button?.title = "✓"
            applyMenu(for: .idle)   // idle menu immediately for back-to-back recordings
            startRestoreTimer()
        }
    }

    private func applyMenu(for state: RecordingState) {
        switch state {
        case .idle, .done: statusItem.menu = idleMenu()
        case .recording:   statusItem.menu = recordingMenu()
        case .finalizing:  statusItem.menu = finalizingMenu()
        }
    }

    // ── Menu construction ──────────────────────────────────────────────────────

    private func idleMenu() -> NSMenu {
        let menu = NSMenu()
        menu.delegate = self
        menu.addItem(item("Start Recording", action: #selector(startRecording)))
        menu.addItem(.separator())
        let openLast = item("Open Last Transcript",
                            action: config.lastTranscript != nil ? #selector(openLast) : nil)
        openLast.isEnabled = config.lastTranscript != nil
        menu.addItem(openLast)
        menu.addItem(.separator())
        menu.addItem(item(config.launchAtLogin ? "✓ Launch at Login" : "Launch at Login",
                          action: #selector(toggleLaunchAtLogin)))
        menu.addItem(item("Settings…", action: #selector(openSettings)))
        menu.addItem(.separator())
        menu.addItem(item("Quit", action: #selector(NSApplication.terminate(_:))))
        return menu
    }

    private func recordingMenu() -> NSMenu {
        let menu = NSMenu()
        menu.delegate = self
        menu.addItem(elapsedItem)
        menu.addItem(.separator())
        menu.addItem(item("View Live Transcript", action: #selector(viewLiveTranscript)))
        menu.addItem(.separator())
        menu.addItem(item("Set Title…", action: #selector(setTitle)))
        menu.addItem(item("Set Save Location…", action: #selector(setSaveLocation)))
        menu.addItem(.separator())
        menu.addItem(item("Stop Recording", action: #selector(stopRecording)))
        return menu
    }

    private func finalizingMenu() -> NSMenu {
        let menu = NSMenu()
        let placeholder = NSMenuItem(title: "Finalizing transcript…", action: nil, keyEquivalent: "")
        placeholder.isEnabled = false
        menu.addItem(placeholder)
        return menu
    }

    private func item(_ title: String, action: Selector?) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: action, keyEquivalent: "")
        i.target = self
        return i
    }

    // ── NSMenuDelegate ─────────────────────────────────────────────────────────

    func menuWillOpen(_ menu: NSMenu) {
        guard case .recording = session.state else { return }
        elapsedItem.title = "Recording \(session.elapsed)"
    }

    // ── Timers ─────────────────────────────────────────────────────────────────

    private func startPulseTimer() {
        let t = DispatchSource.makeTimerSource(queue: .main)
        t.schedule(deadline: .now() + 1, repeating: 1)
        t.setEventHandler { [weak self] in
            guard let self else { return }
            pulseOn.toggle()
            statusItem.button?.title = pulseOn ? "●" : "○"
        }
        t.resume()
        pulseTimer = t
    }

    private func startSpinnerTimer() {
        let t = DispatchSource.makeTimerSource(queue: .main)
        t.schedule(deadline: .now() + 0.1, repeating: 0.1)
        t.setEventHandler { [weak self] in
            guard let self else { return }
            spinnerIdx = (spinnerIdx + 1) % spinnerFrames.count
            statusItem.button?.title = String(spinnerFrames[spinnerIdx])
        }
        t.resume()
        spinnerTimer = t
    }

    private func startRestoreTimer() {
        let t = DispatchSource.makeTimerSource(queue: .main)
        t.schedule(deadline: .now() + 3, repeating: .never)
        t.setEventHandler { [weak self] in self?.session.restoreIdle() }
        t.resume()
        restoreTimer = t
    }

    private func stopAllTimers() {
        pulseTimer?.cancel();   pulseTimer = nil
        spinnerTimer?.cancel(); spinnerTimer = nil
        restoreTimer?.cancel(); restoreTimer = nil
    }

    // ── Actions ────────────────────────────────────────────────────────────────

    @objc private func startRecording() {
        restoreTimer?.cancel(); restoreTimer = nil  // allow Start from DONE state
        if config.outputDirectory == nil {
            pickOutputDirectory { [weak self] in self?.session.start() }
            return
        }
        session.start()
    }

    @objc private func stopRecording() {
        session.stop()
        // Placeholder: complete finalizing immediately until the audio pipeline is wired up
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            self?.session.completeFinalizing()
        }
    }

    @objc private func setTitle() {
        // Phase 6
    }

    @objc private func setSaveLocation() {
        pickOutputDirectory(completion: nil)
    }

    @objc private func viewLiveTranscript() {
        // Phase 4
    }

    @objc private func openLast() {
        guard let path = config.lastTranscript else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    @objc private func openSettings() {
        // Phase 6
    }

    @objc private func toggleLaunchAtLogin() {
        // Phase 6: SMAppService
    }

    // ── Helpers ────────────────────────────────────────────────────────────────

    private func pickOutputDirectory(completion: (() -> Void)?) {
        showDialog {
            let panel = NSOpenPanel()
            panel.canChooseFiles = false
            panel.canChooseDirectories = true
            panel.canCreateDirectories = true
            panel.prompt = "Select"
            panel.message = "Select output directory for transcripts:"
            guard panel.runModal() == .OK, let url = panel.url else { return }
            self.config.outputBookmark = try? url.bookmarkData(
                options: .withSecurityScope,
                includingResourceValuesForKeys: nil,
                relativeTo: nil
            )
            self.config.save()
            completion?()
        }
    }

    private func showDialog(_ fn: () -> Void) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        defer { NSApp.setActivationPolicy(.accessory) }
        fn()
    }
}
