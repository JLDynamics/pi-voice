import Foundation

/// Decide when a tool result may ask the model to continue.
///
/// Parallel tool calls (several `web_search` in one response) must not each
/// fire `response.create`. The first result would be spoken, then later
/// results would trigger another nearly identical reply.
enum VoiceToolFollowUp {
    static func shouldSend(pendingTools: Int, responseActive: Bool) -> Bool {
        pendingTools == 0 && !responseActive
    }

    /// `spawn_thinking` and `stop_thinking` are fire-and-forget: nothing asks
    /// the model to continue after them. The model is told to say a short line
    /// with the call, but in practice it emits the call alone, so asking for
    /// work was answered with silence. When a finished response carried one of
    /// them and spoke nothing, ask once more so Agent acknowledges it. Never
    /// when it already spoke (that would say it twice) or was cancelled, and
    /// not while muted: the extension drops a muted handoff, so "on it" would
    /// promise work that never starts.
    static func shouldAcknowledgeHandoff(handoffCalled: Bool, spokenText: String, cancelled: Bool, muted: Bool = false) -> Bool {
        handoffCalled && !cancelled && !muted && spokenText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
}

/// When Pi's `[FINAL]` / failure context must wait instead of going out now.
///
/// After a spoken turn is transcribed, the server answers it implicitly, but
/// announces `response.created` only with the first output, seconds later.
/// An item sent in that window lands in the chat *before* the reply the
/// server is already generating, and a `response.create` sent with it runs
/// after that reply, so the conversation ends on an assistant message. Claude
/// rejects that ("does not support assistant message prefill"), and the Pi
/// result was never spoken (seen live: a result landed while the user talked).
/// Hold it until the turn's `response.done`, with a cap so a turn the server
/// drops silently cannot strand it.
enum VoiceContextHold {
    static let maxHold: TimeInterval = 15

    static func shouldHold(implicitTurnSince: Date?, now: Date = Date()) -> Bool {
        guard let since = implicitTurnSince else { return false }
        return now.timeIntervalSince(since) < maxHold
    }
}

/// The server's `error` events name their kind in `error.type`
/// (`build_error_event`) and leave `code` null. Matching only `code` turned
/// every `turn_ignored` (an empty transcript) into a request failure that Pi
/// showed as "Error: Turn ignored (no_text) Your message is saved".
enum VoiceServerError {
    static func kind(_ error: [String: Any]) -> String {
        if let code = error["code"] as? String, !code.isEmpty { return code }
        return error["type"] as? String ?? ""
    }
}

/// A start failure that no retry can fix while the process runs.
protocol FatalStartError: Error {}

enum HeadlessExitCode {
    static let audioUnavailable: Int32 = 3
}

enum AudioIOProbe {
    static let timeout: TimeInterval = 2.0
    static let pollInterval: TimeInterval = 0.02
    static let noIOMessage = "No audio input or output is running. Is the lid closed, or is the audio device unavailable?"

    static func waitForFirstCycle(
        timeout: TimeInterval = timeout,
        pollInterval: TimeInterval = pollInterval,
        rendered: () -> Bool,
        sleep: (TimeInterval) async -> Void
    ) async -> Bool {
        if rendered() { return true }
        guard timeout > 0, pollInterval > 0 else { return false }
        var elapsed: TimeInterval = 0
        while elapsed < timeout - 1e-9 {
            let step = min(pollInterval, timeout - elapsed)
            await sleep(step)
            elapsed += step
            if rendered() { return true }
        }
        return false
    }
}

/// When `speech_started` may steal the panel.
///
/// Gaps between TTS chunks look like "not playing". An active response
/// must keep the panel on the agent. Local leftover playback is not
/// interruptible here. Only an admitted turn clears speakers.
enum VoiceSpeechStartPolicy {
    static func shouldShowListening(responseActive: Bool, playing: Bool) -> Bool {
        !responseActive && !playing
    }
}

struct VoiceTurnAdmission: Hashable {
    let id: String
    let itemID: String
    let interruptsOutput: Bool
}

enum VoiceTurnAdmissionPolicy {
    static func shouldApply(id: String, seen: Set<String>, interruptsOutput: Bool) -> Bool {
        interruptsOutput && !id.isEmpty && !seen.contains(id)
    }
}

/// Live mic path that never hops to the MainActor: batch, then a chunk ready
/// for the WebSocket. Mute is a flag the next ingest sees immediately.
///
/// There was an energy gate here, rejecting anything under -48 dBFS. Apple's
/// voice processing already suppresses noise on this path, upstream runs
/// without a second gate, and a loudness rule cannot tell a person in the
/// next room from one at the laptop — both arrive as speech well above any
/// floor quiet enough to keep real talking.
final class MicCapture: @unchecked Sendable {
    static let sendThreshold = 1280

    private let lock = NSLock()
    private var pending: [UInt8] = []
    private var muted = false
    private var accepting = false
    private var generation = UUID()
    private var framesSent = 0

    func arm(generation: UUID) {
        lock.lock()
        defer { lock.unlock() }
        self.generation = generation
        // Mute is SessionController state, reapplied after start. Arming a
        // new generation must not unmute.
        accepting = false
        framesSent = 0
        pending.removeAll(keepingCapacity: true)
    }

    func setAccepting(_ on: Bool) {
        lock.lock()
        accepting = on
        lock.unlock()
    }

    func setMuted(_ on: Bool) {
        lock.lock()
        muted = on
        lock.unlock()
    }

    func disarm() {
        lock.lock()
        defer { lock.unlock() }
        accepting = false
        muted = false
        pending.removeAll(keepingCapacity: true)
        generation = UUID()
    }

    /// Returns a ~40ms PCM16 chunk to send, or nil if muted/not ready.
    func ingest(_ raw: [UInt8], generation: UUID) -> [UInt8]? {
        lock.lock()
        defer { lock.unlock() }
        guard generation == self.generation, accepting, !muted else { return nil }
        pending.append(contentsOf: raw)
        guard pending.count >= Self.sendThreshold else { return nil }
        let chunk = pending
        pending.removeAll(keepingCapacity: true)
        framesSent += 1
        return chunk
    }
}

/// WebSocket send from the mic queue or the MainActor without serializing UI.
final class VoiceSocketSend: @unchecked Sendable {
    private let lock = NSLock()
    private var webSocket: URLSessionWebSocketTask?
    private var generation = UUID()

    func attach(_ ws: URLSessionWebSocketTask?, generation: UUID) {
        lock.lock()
        webSocket = ws
        self.generation = generation
        lock.unlock()
    }

    func send(_ object: [String: Any], generation: UUID? = nil) {
        lock.lock()
        let ws = webSocket
        let current = self.generation
        lock.unlock()
        if let generation, generation != current { return }
        guard let ws,
              JSONSerialization.isValidJSONObject(object),
              let data = try? JSONSerialization.data(withJSONObject: object),
              let text = String(data: data, encoding: .utf8)
        else { return }
        ws.send(.string(text)) { error in
            if let error {
                NSLog("[LiveVoice] send failed: \(error.localizedDescription)")
            }
        }
    }
}

/// Invalidates asynchronous work when a connection or user turn is superseded.
@MainActor
final class VoiceWorkScope {
    private(set) var generation = UUID()
    private var tasks: [String: Task<Void, Never>] = [:]

    var pendingIds: [String] { Array(tasks.keys) }
    func contains(_ id: String) -> Bool { tasks[id] != nil }
    func insert(_ task: Task<Void, Never>, id: String) { tasks[id] = task }
    func finish(_ id: String, generation: UUID) {
        guard self.generation == generation else { return }
        tasks[id] = nil
    }
    func cancel() {
        generation = UUID()
        let obsolete = tasks.values
        tasks.removeAll()
        for task in obsolete { task.cancel() }
    }
}

/// Playback completions from an old queue cannot consume a new queue's buffers.
final class PlaybackTracker: @unchecked Sendable {
    private let lock = NSLock()
    private var generation = UUID()
    private var count = 0
    private var drainedAt = Date.distantPast
    private var firstPlaybackEndedAt: Date?
    private var firstPlaybackStarted = false

    var isAudible: Bool {
        lock.lock(); defer { lock.unlock() }
        return count > 0
    }
    func needsEchoGuard(now: Date = Date()) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return count > 0 || now.timeIntervalSince(drainedAt) < 0.9
    }
    /// Give voice processing one reply to settle before trusting it to reject
    /// the speaker signal. The first reply is where an echoed greeting can
    /// become the user's next turn.
    func needsFirstReplyGuard(now: Date = Date()) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard firstPlaybackStarted else { return false }
        if let ended = firstPlaybackEndedAt { return now.timeIntervalSince(ended) < 0.9 }
        return count > 0
    }
    func resetForSession() {
        lock.lock(); defer { lock.unlock() }
        generation = UUID()
        count = 0
        drainedAt = Date.distantPast
        firstPlaybackEndedAt = nil
        firstPlaybackStarted = false
    }
    func enqueue() -> UUID {
        lock.lock(); defer { lock.unlock() }
        count += 1
        firstPlaybackStarted = true
        return generation
    }
    @discardableResult
    func complete(_ token: UUID, now: Date = Date()) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard token == generation, count > 0 else { return false }
        count -= 1
        if count == 0 {
            drainedAt = now
            if firstPlaybackEndedAt == nil { firstPlaybackEndedAt = now }
        }
        return count == 0
    }
    func clear(now: Date = Date()) {
        lock.lock(); defer { lock.unlock() }
        if count > 0 { drainedAt = now }
        if count > 0 && firstPlaybackEndedAt == nil { firstPlaybackEndedAt = now }
        generation = UUID()
        count = 0
    }
}

/// Keep transport metadata with extracted content, including unsuccessful reads.
enum VoiceToolFormatting {
    static func page(_ json: [String: Any], source: String) -> String {
        var result = json
        result["source"] = source
        let text = (json["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let gated = json["gated"] as? Bool ?? false
        result["status"] = gated ? "blocked" : (json["status"] as? String ?? (text.isEmpty ? "empty" : "read"))
        result["complete"] = !gated && !text.isEmpty
            && !(json["truncated"] as? Bool ?? false) && (json["complete"] as? Bool ?? true)
        guard let data = try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]),
              let output = String(data: data, encoding: .utf8) else { return "Invalid page result." }
        return output
    }
}
