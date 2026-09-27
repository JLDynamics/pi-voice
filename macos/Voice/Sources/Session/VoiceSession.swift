import Foundation
import Combine

enum SessionState: Equatable {
    case idle
    case connecting
    case listening
    /// The user's turn ended and the model is working (transcribing,
    /// reasoning, running a search) but has not started speaking yet.
    case thinking
    case agentSpeaking
    case failed(String)
}

/// Mic/speaker meters update ~30 Hz. Keep them off SessionController so the
/// rest of the panel (buttons, settings, transcript) does not rebuild every tick.
@MainActor
final class AudioLevels: ObservableObject {
    @Published var input: Float = 0
    @Published var output: Float = 0

    func reset() {
        input = 0
        output = 0
    }
}

struct Turn: Identifiable, Equatable {
    enum Speaker { case you, agent }

    let id = UUID()
    let speaker: Speaker
    var text: String
    let at: Date

    init(speaker: Speaker, text: String, at: Date = Date()) {
        self.speaker = speaker
        self.text = text
        self.at = at
    }
}

/// One entry of the running conversation, kept so a turn can be revised and
/// history replayed to the backend. It used to be the sidecar's saved-chat
/// wire format; nothing persists it now, so it lives here with its only user.
public struct ChatMessage: Codable, Equatable {
    public var role: String   // "user" | "assistant" | "tool"
    public var text: String
    public var name: String?  // tool name when role == "tool"

    public init(role: String, text: String, name: String? = nil) {
        self.role = role
        self.text = text
        self.name = name
    }
}

/// The swappable half. Implement this against whatever realtime voice service you
/// choose; nothing above this line touches a network. Build the interface against
/// MockVoiceBackend first — see README.
@MainActor
protocol VoiceBackend: AnyObject {
    var onState: ((SessionState) -> Void)? { get set }
    var onInputLevel: ((Float) -> Void)? { get set }        // 0...1, ~30 Hz
    var onOutputLevel: ((Float) -> Void)? { get set }       // 0...1, ~30 Hz
    /// The user began speaking. Drives the talking indicator, which stands in
    /// for a live transcript, which the pipeline no longer produces.
    var onUserSpeechStarted: (() -> Void)? { get set }
    /// The server finalized a turn but will not answer it (turn_ignored).
    var onTurnDropped: (() -> Void)? { get set }
    var onUserFinal: ((String, String?) -> Void)? { get set } // final transcript + server item id (stable across pause-merged segments)
    var onAgentDelta: ((String) -> Void)? { get set }       // streamed reply text
    var onAgentDone: (() -> Void)? { get set }
    var onToolActive: ((String) -> Void)? { get set }
    /// (tool name, short result summary) — the summary is recorded in the
    /// transcript so later turns can see what a tool actually returned.
    var onToolDone: ((String, String) -> Void)? { get set }
    var onAudioStatus: ((String?) -> Void)? { get set }
    var onToolsCancelled: (() -> Void)? { get set }

    func start() async throws
    func stop() async
    func setMuted(_ muted: Bool)
    func interrupt()
    func speak(_ text: String)
    /// Saved transcript to replay into the live conversation once the server
    /// acknowledges the session (mirrors the web `_replayHistory`, last 20).
    /// Each entry is (role, text) with role in user/assistant/tool.
    func setHistory(_ messages: [(role: String, text: String, name: String?)])
    /// Push the current Settings tool toggles into a live session.
    func refreshTools()
}

extension VoiceBackend {
    func setHistory(_ messages: [(role: String, text: String, name: String?)]) {}
    func refreshTools() {}
    func speak(_ text: String) {}
}

/// What the interface observes. Owns the transcript and the visible state; the
/// backend only feeds it events.
@MainActor
final class SessionController: ObservableObject {

    @Published private(set) var state: SessionState = .idle
    @Published private(set) var turns: [Turn] = []
    /// True from the moment speech is detected until the turn's text is painted
    /// or dropped. Drives the talking indicator that stands in for a live
    /// transcript: streaming ASR text necessarily revises itself, so the panel
    /// shows that you are talking rather than an unstable guess at the words.
    @Published private(set) var userSpeaking = false
    @Published private(set) var activeTool: String?
    let levels = AudioLevels()

    /// A finalized transcript waiting for its turn to be over. Pausing mid-turn
    /// makes the server finalize each revision, so painting every one made the
    /// bubble rewrite itself while the user was still talking. Revisions
    /// overwrite this; it is painted once, when the turn settles.
    private var pendingUserText: String?
    private var pendingUserItemId: String?
    @Published private(set) var isMuted = false

    /// Shown as one line in the header. Cleared on the next successful start.
    @Published private(set) var errorText: String?
    @Published private(set) var audioStatus: String?
    private var userRows: [String: UUID] = [:]
    private var userMessages: [String: Int] = [:]

    // MARK: - Transcript state
    //
    // The running conversation is still tracked here so a turn can be revised
    // and history replayed to the backend. It is no longer persisted: the
    // sidecar that stored saved chats and personal memory was removed.

    private var messages: [ChatMessage] = []
    private var pendingAgentText = ""
    private var lastUserItemId: String?
    private var lastUserActivityAt: Date?
    private static let pauseMergeWindow: TimeInterval = 4
    private static let fillerAgentLimit = 24

    var isLive: Bool {
        switch state {
        case .idle, .failed: return false
        case .connecting, .listening, .thinking, .agentSpeaking: return true
        }
    }

    let backend: VoiceBackend
    private let maxTurns = 200

    init(backend: VoiceBackend) {
        self.backend = backend
        wire()
    }

    private func wire() {
        backend.onState = { [weak self] state in
            guard let self else { return }
            self.state = state
            if case .failed(let message) = state { self.errorText = message }
        }
        backend.onUserSpeechStarted = { [weak self] in
            guard let self else { return }
            self.userSpeaking = true
            self.markUserActivity()
        }
        backend.onTurnDropped = { [weak self] in
            // The server finalized this turn but will not answer it. Whatever it
            // chose to send is all we will get, so paint it and stop indicating.
            self?.flushPendingUser()
            self?.userSpeaking = false
        }
        backend.onInputLevel = { [weak self] level in self?.levels.input = level }
        backend.onOutputLevel = { [weak self] level in self?.levels.output = level }

        backend.onAudioStatus = { [weak self] note in self?.audioStatus = note }
        backend.onToolsCancelled = { [weak self] in self?.activeTool = nil }
        backend.onUserFinal = { [weak self] text, itemId in
            guard let self else { return }
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty else { return }
            // A final for a different item means the held turn is definitively
            // over, so it can be painted before this one starts being held.
            if let held = self.pendingUserItemId, held != itemId {
                self.flushPendingUser()
            }
            self.pendingUserText = trimmed
            self.pendingUserItemId = itemId
            self.markUserActivity()
        }

        // Deltas stream into a single agent turn rather than appending rows.
        backend.onAgentDelta = { [weak self] chunk in
            guard let self else { return }
            // The agent is answering this turn, so the turn is settled: paint
            // the user's words before the reply lands under them.
            self.flushPendingUser()
            self.userTurnOpen = false
            self.pendingAgentText += chunk
            if let last = self.turns.last, last.speaker == .agent, self.agentTurnOpen {
                self.turns[self.turns.count - 1].text += chunk
            } else {
                self.agentTurnOpen = true
                self.append(Turn(speaker: .agent, text: chunk))
            }
        }
        backend.onAgentDone = { [weak self] in
            guard let self else { return }
            // A reply that produced no text still settles the turn.
            self.flushPendingUser()
            self.agentTurnOpen = false
            self.userTurnOpen = false
            let text = self.pendingAgentText.trimmingCharacters(in: .whitespacesAndNewlines)
            self.pendingAgentText = ""
            if !text.isEmpty { self.recordAssistant(text) }
        }
        backend.onToolActive = { [weak self] name in
            self?.flushPendingUser()
            self?.userTurnOpen = false
            let desc: String
            switch name {
            case "bash": desc = "Fetching the web…"
            default: desc = "Running \(name)…"
            }
            self?.activeTool = desc
        }
        backend.onToolDone = { [weak self] name, summary in
            self?.activeTool = nil
            // replayHistory already renders a "tool" turn as "[Earlier I used
            // X] ..." but nothing ever wrote one, so the model's own history
            // showed it narrating actions with no record of the result. That
            // gap is what let it describe work it had not managed to do.
            self?.recordTool(name: name, summary: summary)
        }
    }

    private var agentTurnOpen = false
    /// True while the last visible turn is the user's still-open turn: later
    /// finals for the same spoken stretch (pause-split revisions, even with a
    /// fresh item id) extend it instead of appending duplicate bubbles.
    /// Cleared by any assistant/agent/tool activity or session change.
    private var userTurnOpen = false
    private func append(_ turn: Turn) {
        if turn.speaker != .you { userTurnOpen = false }
        turns.append(turn)
        if turns.count > maxTurns { turns.removeFirst(turns.count - maxTurns) }
    }

    // MARK: - Intents

    private var isTransitioning = false
    private var beginTask: Task<Void, Never>?

    func toggleSession() {
        // Allow tapping End while a connect is still in flight;
        // otherwise the orb looks dead for up to the 8s handshake timeout.
        if isLive {
            requestEnd()
            return
        }
        guard !isTransitioning else { return }
        errorText = nil
        state = .connecting
        Task { await begin() }
    }

    /// Paint idle on this click, then tear down and save without blocking the button.
    func requestEnd() {
        beginTask?.cancel()
        applyIdleChrome()
        Task { await end() }
    }

    func begin() async {
        guard !isTransitioning else { return }
        userTurnOpen = false
        isTransitioning = true
        errorText = nil
        if state != .connecting { state = .connecting }
        beginTask?.cancel()
        // Hand the saved transcript to the backend so the live session opens
        // with the saved context.
        backend.setHistory(messages.map { ($0.role, $0.text, $0.name) })
        // Capture the task so a tap-to-cancel during connect can interrupt it.
        let task = Task { @MainActor in
            do {
                try await backend.start()
            } catch is CancellationError {
                // Cancelled via toggle during connecting; backend.stop() in end() cleans up.
            } catch {
                state = .failed(error.localizedDescription)
                errorText = error.localizedDescription
            }
        }
        beginTask = task
        await task.value
        beginTask = nil
        let cancelled = task.isCancelled
        isTransitioning = false
        if cancelled {
            await backend.stop()
            applyIdleChrome()
            return
        }
        if errorText == nil, isMuted {
            backend.setMuted(true)
        }
    }

    func end() async {
        beginTask?.cancel()
        beginTask = nil
        // Keep a cut-off reply in history like the web "[interrupted]" marker.
        let leftover = pendingAgentText.trimmingCharacters(in: .whitespacesAndNewlines)
        pendingAgentText = ""
        if !leftover.isEmpty { recordAssistant(leftover + " [interrupted]") }
        applyIdleChrome()
        if isTransitioning {
            await backend.stop()
            await flushSave()
            return
        }
        isTransitioning = true
        defer { isTransitioning = false }
        await backend.stop()
        await flushSave()
    }

    /// Mute, Stop, and hide must paint idle before teardown or a sidecar save.
    private func applyIdleChrome() {
        flushPendingUser()
        userSpeaking = false
        agentTurnOpen = false
        userTurnOpen = false
        levels.reset()
        activeTool = nil
        state = .idle
    }

    func setMuted(_ muted: Bool) {
        guard isLive else { return }
        isMuted = muted
        backend.setMuted(muted)
        if muted { levels.input = 0 }
    }

    func speak(_ text: String) {
        backend.speak(text)
    }

    /// Only wired up if Stop is a barge-in rather than an end — see SPEC.md §7.
    func interrupt() {
        guard isLive, state != .connecting else { return }
        backend.interrupt()
        activeTool = nil
    }

    // MARK: - Transcript

    private func recordUser(_ text: String) {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        messages.append(ChatMessage(role: "user", text: t))
    }

    /// Merge pause-split finals: the server usually reuses one item id across
    /// reopened segments of a single turn, so a matching id updates the
    /// existing bubble instead of appending a duplicate. The open-turn flag
    /// covers the rest: while no assistant reply intervened, a later final
    /// extends the same bubble even with a fresh id. The continuation rule
    /// below covers the last gap: the assistant answered the partial before
    /// you finished, so the restated final extends an earlier bubble with
    /// other rows in between. One utterance still reads as one bubble.
    /// Paint the held transcript and stop indicating. Called only when the turn
    /// is settled: the agent started answering it, the reply finished, the
    /// server dropped it, or a different turn finalized after it.
    private func flushPendingUser() {
        guard let text = pendingUserText else { return }
        let itemId = pendingUserItemId
        pendingUserText = nil
        pendingUserItemId = nil
        userSpeaking = false
        upsertUserTurn(text: text, itemId: itemId)
    }

    private func upsertUserTurn(text: String, itemId: String?) {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        markUserActivity()
        if let id = itemId, let row = userRows[id],
           let index = turns.firstIndex(where: { $0.id == row }) {
            // Same item id means the same turn, and a revision's final is
            // decoded from all of that turn's audio. It replaces the bubble;
            // merging it in appended one more copy of the turn per pause.
            turns[index].text = t
            if let messageIndex = userMessages[id], messages.indices.contains(messageIndex) {
                messages[messageIndex].text = turns[index].text
            }
            lastUserItemId = id
            return
        }
        if let last = turns.last, last.speaker == .you, userTurnOpen {
            turns[turns.count - 1].text = Self.mergedUserText(current: last.text, next: t)
            if let idx = messages.lastIndex(where: { $0.role == "user" }) {
                messages[idx].text = turns[turns.count - 1].text
            }
            lastUserItemId = itemId
            bindUserItem(itemId)
            userTurnOpen = true
            return
        }
        if let idx = turns.lastIndex(where: { $0.speaker == .you }),
           turns.count - 1 - idx <= 2,
           Self.isContinuation(of: turns[idx].text, next: t) {
            turns[idx].text = t
            if let mIdx = messages.lastIndex(where: { $0.role == "user" }) {
                messages[mIdx].text = t
            }
            lastUserItemId = itemId
            bindUserItem(itemId)
            userTurnOpen = true
            return
        }
        if shouldMergePausedContinuation(),
           let idx = turns.lastIndex(where: { $0.speaker == .you }) {
            turns[idx].text = Self.mergedUserText(current: turns[idx].text, next: t)
            if let mIdx = messages.lastIndex(where: { $0.role == "user" }) {
                messages[mIdx].text = turns[idx].text
            }
            lastUserItemId = itemId
            bindUserItem(itemId)
            userTurnOpen = true
            return
        }
        lastUserItemId = itemId
        userTurnOpen = true
        append(Turn(speaker: .you, text: t))
        recordUser(t)
        bindUserItem(itemId)
    }

    private func bindUserItem(_ itemId: String?) {
        guard let id = itemId, let row = turns.last(where: { $0.speaker == .you }),
              let index = messages.lastIndex(where: { $0.role == "user" }) else { return }
        userRows[id] = row.id
        userMessages[id] = index
    }

    private func markUserActivity() {
        lastUserActivityAt = Date()
    }

    private func isRecentUserActivity() -> Bool {
        guard let at = lastUserActivityAt else { return false }
        return Date().timeIntervalSince(at) < Self.pauseMergeWindow
    }

    private func trailingAgentIsFiller() -> Bool {
        guard let last = turns.last, last.speaker == .agent else { return false }
        let text = last.text.trimmingCharacters(in: .whitespacesAndNewlines)
        return text.count < Self.fillerAgentLimit
    }

    /// A thinking pause can finalize the first clause, let the model start a
    /// tiny reply, then the rest of the sentence arrives as a new item id.
    /// Keep that as one bubble when the agent has not actually answered yet.
    private func shouldMergePausedContinuation() -> Bool {
        guard isRecentUserActivity(),
              turns.contains(where: { $0.speaker == .you }) else { return false }
        if turns.last?.speaker == .you { return true }
        return trailingAgentIsFiller()
    }

    /// Merge a later hypothesis into text already shown for this utterance.
    /// A restated whole replaces the bubble; a new pause fragment is appended
    /// so the first words cannot vanish.
    private static func mergedUserText(current: String, next: String) -> String {
        let a = current.trimmingCharacters(in: .whitespacesAndNewlines)
        let b = next.trimmingCharacters(in: .whitespacesAndNewlines)
        if a.isEmpty { return b }
        if b.isEmpty { return a }
        if isContinuation(of: a, next: b) { return b }
        let al = a.lowercased()
        let bl = b.lowercased()
        if bl.contains(al) && b.count >= a.count { return b }
        if al.contains(bl) { return a }
        let aw = a.split(whereSeparator: { $0.isWhitespace }).map(String.init)
        let bw = b.split(whereSeparator: { $0.isWhitespace }).map(String.init)
        let maxK = min(aw.count, bw.count)
        if maxK >= 2 {
            for k in stride(from: maxK, through: 2, by: -1) {
                let tail = aw.suffix(k).map { $0.lowercased() }
                let head = bw.prefix(k).map { $0.lowercased() }
                if tail == Array(head) {
                    return (aw + bw.dropFirst(k)).joined(separator: " ")
                }
            }
        }
        return a + " " + b
    }

    /// Whether a new final restates and extends an earlier partial bubble:
    /// same words (STT may revise the spelling), or the old bubble is a
    /// prefix of the restated whole. Adjacency is enforced by the caller.
    private static func isContinuation(of old: String, next newText: String) -> Bool {
        let a = old.lowercased().replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let b = newText.lowercased().replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !a.isEmpty, !b.isEmpty else { return false }
        if a == b || (b.count > a.count && b.hasPrefix(a)) { return true }
        let oldWords = a.split(separator: " ")
        let newWords = b.split(separator: " ")
        let stem = min(3, oldWords.count)
        guard stem >= 2 else { return false }
        guard zip(oldWords.prefix(stem), newWords.prefix(stem)).allSatisfy({ $0 == $1 }) else {
            return false
        }
        // Final STT often restates the same utterance a word shorter or longer.
        return newWords.count + 2 >= oldWords.count
    }

    /// Append a tool result to the transcript so it survives into later turns.
    private func recordTool(name: String, summary: String) {
        let trimmed = summary.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        // Keep transcripts lean: a fetch can return 20k characters and the
        // replay only shows the first 500 anyway.
        let capped = trimmed.count > 2000 ? String(trimmed.prefix(1997)) + "..." : trimmed
        userTurnOpen = false
        messages.append(ChatMessage(role: "tool", text: capped, name: name))
    }

    private func recordAssistant(_ text: String) {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        messages.append(ChatMessage(role: "assistant", text: t))
    }

    /// Conversations were persisted to the sidecar; nothing stores them now.
    /// Kept as a no-op so the end/teardown paths still read as "flush, then
    /// stop" and gain a store again without re-threading every call site.
    func flushSave() async {}
}
