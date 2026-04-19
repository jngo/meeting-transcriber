import Foundation

enum RecordingState {
    case idle
    case recording(startedAt: Date)
    case finalizing
    case done
}

final class RecordingSession {
    private(set) var state: RecordingState = .idle
    var onStateChange: ((RecordingState) -> Void)?

    var elapsed: String {
        guard case .recording(let startedAt) = state else { return "0:00:00" }
        let total = Int(Date().timeIntervalSince(startedAt))
        return String(format: "%d:%02d:%02d", total / 3600, (total % 3600) / 60, total % 60)
    }

    func start() {
        switch state {
        case .idle, .done: transition(to: .recording(startedAt: Date()))
        default: break
        }
    }

    func stop() {
        guard case .recording = state else { return }
        transition(to: .finalizing)
    }

    func completeFinalizing() {
        guard case .finalizing = state else { return }
        transition(to: .done)
    }

    func restoreIdle() {
        guard case .done = state else { return }
        transition(to: .idle)
    }

    private func transition(to newState: RecordingState) {
        state = newState
        onStateChange?(newState)
    }
}
