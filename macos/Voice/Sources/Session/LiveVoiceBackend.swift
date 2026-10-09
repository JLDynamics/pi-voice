import AVFoundation
import Darwin
import Foundation

/// Realtime WebSocket backend for the Chatbot voice server
/// (`ws://127.0.0.1:8766/v1/realtime`).
@MainActor
final class LiveVoiceBackend: VoiceBackend, HeadlessBackend {

    var onState: ((SessionState) -> Void)?
    var onUserSpeechStarted: (() -> Void)?
    var onTurnDropped: (() -> Void)?
    var onRequestError: ((String) -> Void)?
    var onUserFinal: ((String, String?) -> Void)?
    var onAgentDelta: ((String) -> Void)?
    var onAgentDone: (() -> Void)?
    var onToolActive: ((String) -> Void)?
    var onToolDone: ((String, String) -> Void)?
    var onToolsCancelled: (() -> Void)?
    var onSpoken: ((String) -> Void)?
    var onSpawnThinking: ((String, String) -> Void)?
    var onStopThinking: (() -> Void)?

    enum Connection {
        case idle
        case starting
        case awaitingSession
        case ready
    }

    let wsURL: URL
    let voice: String
    let instructions: String

    let audio = AudioEngine()
    let pcm = PCMBridge()
    let mic = MicCapture()
    let socket = VoiceSocketSend()
    let micQueue = DispatchQueue(label: "dev.jldynamics.Voice.mic", qos: .userInteractive)
    var webSocket: URLSessionWebSocketTask?
    var receiveTask: Task<Void, Never>?
    var handshakeTimeout: Task<Void, Never>?

    var connection: Connection = .idle
    var muted = false
    var closed = true
    var connectionGeneration = UUID()
    let toolScope = VoiceWorkScope()
    var piJobs = PiJobTracker()
    var seenToolCalls = Set<String>()
    var responseCreateRequested = false
    /// Item id of the last finalized user turn, so an empty final can be
    /// matched to the turn it belongs to.
    var lastInputItemId: String?
    /// Server-run tool calls in flight, by call_id, so the matching output can
    /// be reported under the tool's name.
    var serverToolNames: [String: String] = [:]
    /// call_id of a spawn_thinking/stop_thinking in the response now streaming,
    /// so `response.done` can ask for an acknowledgement if it spoke nothing.
    var handoffAckCallId: String?
    /// Set when a spoken turn is transcribed (the server is about to answer it
    /// without a `response.created` yet); cleared when that response starts or
    /// ends. See ``VoiceContextHold``.
    var implicitTurnSince: Date?
    /// Pi context (`[FINAL]`, failure) held during that window, in order.
    var heldContext: [String] = []
    var heldNeedsFollowUp = false

    var agentText = ""
    var activeResponseId = ""
    /// A response.create that arrived while one was still streaming, deferred
    /// until it finishes. The server rejects an overlapping create outright, so
    /// sending it eagerly silently ended the turn.
    var responseRequestPending = false
    var piProgressTask: Task<Void, Never>?
    var lastPiProgressAt = Date.distantPast
    var lastPiProgressNote: String?
    var userSpeechActive = false
    /// Last time the server confirmed a new response. Arms the tool
    /// follow-up watchdog: if no response starts within seconds of a tool
    /// follow-up request, the create is re-sent once.
    var lastResponseCreatedAt = Date.distantPast
    var cancelledIds = Set<String>()
    var seenAdmissionIds = Set<String>()
    /// Saved transcript, set by the controller before start() so the live
    /// session opens with the recent conversation in context.
    var historyMessages: [(role: String, text: String, name: String?)] = []

    init(
        url: URL,
        // Must name a voice this Mac has. "en-Emma_woman" was a default left
        // from an earlier TTS backend; the server rejected it and warned on
        // every utterance (30 times in one session) before falling back here.
        voice: String = "en-US-F",
        instructions: String = """
        You are an AI conversation partner: perceptive, relaxed, warm, and quietly playful. You enjoy exploring ideas and have something thoughtful to contribute. Speak with the ease of someone comfortable in the conversation.
        """
    ) {
        self.wsURL = url
        self.voice = voice
        self.instructions = instructions
    }

    func setHistory(_ messages: [(role: String, text: String, name: String?)]) {
        historyMessages = messages
    }

    func refreshTools() {
        guard !closed, connection == .ready || connection == .awaitingSession else { return }
        sendSessionUpdate()
    }

    func start() async throws {
        await teardown(emitIdle: false)
        closed = false
        connection = .starting
        let generation = connectionGeneration
        mic.arm(generation: generation)
        mic.setMuted(muted)
        onState?(.connecting)

        // Snapshot the mic gain once per session so the ~50 Hz tap never
        // touches UserDefaults (see PCMBridge.micGain).
        let configuredGain = UserDefaults.standard.object(forKey: "voice.micGain") as? Double ?? 0
        pcm.micGain = configuredGain > 0 ? Float(configuredGain) : 3.0

        do {
            try await LocalServiceStarter.shared.ensureReady(voice: wsURL)
        } catch {
            if connectionGeneration == generation { await teardown(emitIdle: false) }
            throw error
        }
        guard !closed, connectionGeneration == generation, !Task.isCancelled else { return }

        // Open the socket before the audio engine: the TCP/WebSocket handshake
        // and session.created happen on the network while voice-processing
        // setup (a few hundred ms) runs on this actor, instead of one after
        // the other. Mic frames only flow once `connection == .ready`, and a
        // socket failure meanwhile tears everything down via fail().
        openWebSocket(generation: generation)

        do {
            try await audio.start()
        } catch {
            fail(error.localizedDescription)
            throw error
        }

        if closed || generation != connectionGeneration || Task.isCancelled {
            audio.stop()
            return
        }

        audio.onPlaybackDrained = { [weak self] in
            Task { @MainActor in
                guard let self, !self.closed, self.connectionGeneration == generation,
                      self.activeResponseId.isEmpty, !self.audio.isPlaying else { return }
                self.onState?(.listening)
                if self.responseRequestPending && self.toolScope.pendingIds.isEmpty {
                    self.responseRequestPending = false
                    self.sendResponseCreate()
                }
            }
        }
        let bridge = pcm
        let capture = mic
        let sink = socket
        let queue = micQueue
        audio.onBuffer = { buffer in
            guard let bytes = bridge.micPCM16(from: buffer) else {
                NSLog("[Tap] micPCM16 returned nil for frames=\(buffer.frameLength)")
                return
            }
            queue.async {
                guard let chunk = capture.ingest(bytes, generation: generation) else { return }
                sink.send(
                    ["type": "input_audio_buffer.append", "audio": Data(chunk).base64EncodedString()],
                    generation: generation
                )
            }
        }
    }

    func openWebSocket(generation: UUID) {
        let task = URLSession.shared.webSocketTask(with: wsURL)
        webSocket = task
        socket.attach(task, generation: generation)
        connection = .awaitingSession
        task.resume()
        let decoder = pcm
        receiveTask = Task.detached { [weak self] in
            await Self.pump(task, owner: self, pcm: decoder, generation: generation)
        }
        handshakeTimeout = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 8_000_000_000)
            guard !Task.isCancelled, let self, self.connectionGeneration == generation,
                  self.connection == .awaitingSession else { return }
            self.fail("Can't reach the service")
        }
    }

    func stop() async {
        await teardown(emitIdle: true)
    }

    func setMuted(_ muted: Bool) {
        self.muted = muted
        mic.setMuted(muted)
        audio.setMuted(muted)
    }

    func interrupt() {
        cancelToolWork()
        rememberCancelled(activeResponseId)
        send(["type": "response.cancel"])
        audio.clearPlayback()
        finishAgentTurn()
        if !closed { onState?(.listening) }
        // response.cancel also drops a pending implicit turn on the server.
        releaseHeldContext()
    }

    /// Send Pi context now, or hold it while the server is answering a spoken
    /// turn it has not announced yet (``VoiceContextHold``).
    func deliverContext(_ text: String, followUp: Bool) {
        if VoiceContextHold.shouldHold(implicitTurnSince: implicitTurnSince) {
            heldContext.append(text)
            if followUp { heldNeedsFollowUp = true }
            let since = implicitTurnSince
            let generation = connectionGeneration
            DispatchQueue.main.asyncAfter(deadline: .now() + VoiceContextHold.maxHold) { [weak self] in
                guard let self, self.connectionGeneration == generation,
                      self.implicitTurnSince == since, !self.heldContext.isEmpty else { return }
                NSLog("[LiveVoice] implicit turn never reported; releasing held context")
                self.releaseHeldContext()
            }
            return
        }
        sendUserText(text)
        if followUp { requestFollowUpIfIdle() }
    }

    func releaseHeldContext() {
        implicitTurnSince = nil
        guard !heldContext.isEmpty else { return }
        let items = heldContext
        let followUp = heldNeedsFollowUp
        heldContext = []
        heldNeedsFollowUp = false
        guard !closed else { return }
        items.forEach { sendUserText($0) }
        if followUp { requestFollowUpIfIdle() }
    }

    func speak(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !closed else { return }
        interrupt()
        send(["type": "response.speak", "text": trimmed])
    }

    func ingestUserText(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !closed else { return }
        sendUserText(trimmed)
        requestFollowUpIfIdle()
    }

    func sendUserText(_ text: String) {
        send([
            "type": "conversation.item.create",
            "item": [
                "type": "message",
                "role": "user",
                "content": [["type": "input_text", "text": text]],
            ] as [String: Any],
        ])
    }

    func requestFollowUpIfIdle() {
        if activeResponseId.isEmpty && !responseCreateRequested && !audio.isPlaying {
            sendResponseCreate()
        } else {
            responseRequestPending = true
        }
    }

    func teardown(emitIdle: Bool) async {
        piProgressTask?.cancel()
        piProgressTask = nil
        closed = true
        connectionGeneration = UUID()
        cancelToolWork()
        seenToolCalls.removeAll()
        responseCreateRequested = false
        lastInputItemId = nil
        connection = .idle
        handshakeTimeout?.cancel()
        handshakeTimeout = nil
        receiveTask?.cancel()
        receiveTask = nil
        webSocket?.cancel(with: .goingAway, reason: nil)
        webSocket = nil
        socket.attach(nil, generation: connectionGeneration)
        audio.onBuffer = nil
        audio.onPlaybackDrained = nil
        audio.stop()
        pcm.reset()
        mic.disarm()
        agentText = ""
        activeResponseId = ""
        responseRequestPending = false
        cancelledIds.removeAll()
        muted = false
        implicitTurnSince = nil
        heldContext = []
        heldNeedsFollowUp = false
        if emitIdle { onState?(.idle) }
    }

    func fail(_ message: String) {
        guard !closed else { return }
        closed = true
        connectionGeneration = UUID()
        cancelToolWork()
        seenToolCalls.removeAll()
        responseCreateRequested = false
        lastInputItemId = nil
        connection = .idle
        handshakeTimeout?.cancel()
        handshakeTimeout = nil
        receiveTask?.cancel()
        receiveTask = nil
        webSocket?.cancel(with: .goingAway, reason: nil)
        webSocket = nil
        socket.attach(nil, generation: connectionGeneration)
        audio.onBuffer = nil
        audio.onPlaybackDrained = nil
        audio.stop()
        pcm.reset()
        mic.disarm()
        onState?(.failed(message))
    }

}
