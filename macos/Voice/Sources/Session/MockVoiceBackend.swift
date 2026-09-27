import Foundation

/// Replays a scripted conversation for session and bridge tests.
@MainActor
final class MockVoiceBackend: VoiceBackend, HeadlessBackend {

    var onState: ((SessionState) -> Void)?
    var onUserSpeechStarted: (() -> Void)?
    var onTurnDropped: (() -> Void)?
    var onUserFinal: ((String, String?) -> Void)?
    var onAgentDelta: ((String) -> Void)?
    var onAgentDone: (() -> Void)?
    var onToolActive: ((String) -> Void)?
    var onToolDone: ((String, String) -> Void)?
    var onToolsCancelled: (() -> Void)?
    var onSpoken: ((String) -> Void)?
    var onAskPi: ((String, String) -> Void)?
    var onStopPi: (() -> Void)?
    var interruptCount = 0
    var ingestedUserText: [String] = []
    var postedResults: [(id: String, speak: String, full: String)] = []

    func ingestUserText(_ text: String) {
        ingestedUserText.append(text)
    }

    func postResult(id: String, speak: String, full: String) {
        postedResults.append((id: id, speak: speak, full: full))
    }

    var piJobUpdates: [(id: String, status: String, note: String?)] = []

    func updatePiJob(id: String, status: String, note: String?) {
        piJobUpdates.append((id: id, status: status, note: note))
    }

    var historyMessages: [(role: String, text: String, name: String?)] = []

    func setHistory(_ messages: [(role: String, text: String, name: String?)]) {
        historyMessages = messages
    }

    private struct Exchange {
        let said: String
        let reply: String
    }

    private let script: [Exchange] = [
        Exchange(said: "What's left on my list today?",
                 reply: "Two things — the vendor call at 3:30, and the freezer count."),
        Exchange(said: "Move the vendor call to four.",
                 reply: "Done, it's at 4:00 now. Want me to let Dana know?")
    ]

    private var running: Task<Void, Never>?

    func start() async throws {
        onState?(.connecting)
        try? await Task.sleep(nanoseconds: 700_000_000)
        running = Task { await self.play() }
    }

    func stop() async {
        running?.cancel(); running = nil
        onState?(.idle)
    }

    func setMuted(_ muted: Bool) {}

    func interrupt() {
        interruptCount += 1
        running?.cancel()
        onAgentDone?()
        onState?(.listening)
    }

    // MARK: - Playback

    private func play() async {
        for exchange in script {
            if Task.isCancelled { return }
            onState?(.listening)

            // Show the talking indicator, then commit the line the way the
            // real backend does: one finalized transcript per turn.
            onUserSpeechStarted?()
            try? await Task.sleep(nanoseconds: 700_000_000)
            if Task.isCancelled { return }
            onUserFinal?(exchange.said, nil)

            if Task.isCancelled { return }
            onState?(.agentSpeaking)
            try? await Task.sleep(nanoseconds: 400_000_000)

            for word in exchange.reply.split(separator: " ") {
                if Task.isCancelled { return }
                onAgentDelta?((exchange.reply.hasPrefix(String(word)) ? "" : " ") + word)
                try? await Task.sleep(nanoseconds: 120_000_000)
            }
            onAgentDone?()
            try? await Task.sleep(nanoseconds: 800_000_000)
        }

        if !Task.isCancelled { onState?(.listening) }
    }

}
