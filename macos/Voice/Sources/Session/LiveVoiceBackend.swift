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

    private enum Connection {
        case idle
        case starting
        case awaitingSession
        case ready
    }

    private let wsURL: URL
    private let voice: String
    private let instructions: String

    private let audio = AudioEngine()
    private let pcm = PCMBridge()
    private let mic = MicCapture()
    private let socket = VoiceSocketSend()
    private let micQueue = DispatchQueue(label: "dev.jldynamics.Voice.mic", qos: .userInteractive)
    private var webSocket: URLSessionWebSocketTask?
    private var receiveTask: Task<Void, Never>?
    private var handshakeTimeout: Task<Void, Never>?

    private var connection: Connection = .idle
    private var muted = false
    private var closed = true
    private var connectionGeneration = UUID()
    private let toolScope = VoiceWorkScope()
    private var piJobs = PiJobTracker()
    private var seenToolCalls = Set<String>()
    private var responseCreateRequested = false
    /// Item id of the last finalized user turn, so an empty final can be
    /// matched to the turn it belongs to.
    private var lastInputItemId: String?
    /// Server-run tool calls in flight, by call_id, so the matching output can
    /// be reported under the tool's name.
    private var serverToolNames: [String: String] = [:]
    /// call_id of a spawn_thinking/stop_thinking in the response now streaming,
    /// so `response.done` can ask for an acknowledgement if it spoke nothing.
    private var handoffAckCallId: String?
    /// Set when a spoken turn is transcribed (the server is about to answer it
    /// without a `response.created` yet); cleared when that response starts or
    /// ends. See ``VoiceContextHold``.
    private var implicitTurnSince: Date?
    /// Pi context (`[FINAL]`, failure) held during that window, in order.
    private var heldContext: [String] = []
    private var heldNeedsFollowUp = false

    private var agentText = ""
    private var activeResponseId = ""
    /// A response.create that arrived while one was still streaming, deferred
    /// until it finishes. The server rejects an overlapping create outright, so
    /// sending it eagerly silently ended the turn.
    private var responseRequestPending = false
    private var piProgressTask: Task<Void, Never>?
    private var lastPiProgressAt = Date.distantPast
    private var lastPiProgressNote: String?
    private var userSpeechActive = false
    /// Last time the server confirmed a new response. Arms the tool
    /// follow-up watchdog: if no response starts within seconds of a tool
    /// follow-up request, the create is re-sent once.
    private var lastResponseCreatedAt = Date.distantPast
    private var cancelledIds = Set<String>()
    private var seenAdmissionIds = Set<String>()
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

    private func openWebSocket(generation: UUID) {
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
    private func deliverContext(_ text: String, followUp: Bool) {
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

    private func releaseHeldContext() {
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

    /// Pi's finished work, on the final channel for Agent to speak.
    ///
    /// Tagged, because it arrives on the same channel as the user's own words
    /// and is otherwise indistinguishable from them — the model would answer
    /// Pi's report as though the user had just said it. The voice prompt
    /// explains `[FINAL]`. The full text is cached so a stopped or failed job
    /// is not rewritten as done when findings arrive with it.
    func postResult(id: String, speak: String, full: String) {
        let trimmed = speak.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !closed, !id.isEmpty else { return }
        let complete = full.trimmingCharacters(in: .whitespacesAndNewlines)
        piJobs.finish(id: id, text: complete.isEmpty ? trimmed : complete)
        piProgressTask?.cancel()
        piProgressTask = nil
        appendPiJournal(PiJobTracker.journalLine(
            now: Date(), event: "result", id: id,
            brief: piJobs.jobs[id]?.brief ?? "",
            extra: ["chars": complete.count]
        ))
        let outcome = piJobs.jobs[id]?.state
        deliverContext(PiJobTracker.finalChannel(excerpt: trimmed, outcome: outcome), followUp: true)
    }

    /// Extension-pushed job phase (`job_update` over stdio). Feeds the local
    /// mirror the status channel speaks from. Only `failed` speaks on its
    /// own. `dropped` stays silent: the dispatch never reached Pi (muted), so
    /// there is no failure to report.
    func updatePiJob(id: String, status: String, note: String?) {
        let state: PiJobTracker.State
        switch status {
        case "queued": state = .queued
        case "working": state = .working
        case "done": state = .done
        case "stopped": state = .stopped
        case "superseded": state = .superseded
        case "dropped": state = .dropped
        case "failed": state = .failed
        default: return
        }
        let previousState = piJobs.jobs[id]?.state
        piJobs.update(id: id, state: state, note: note)
        guard piJobs.jobs[id]?.state == state, previousState != state || state == .working else { return }
        if state == .stopped || state == .superseded || state == .dropped || state == .failed || state == .done {
            if !piJobs.hasActive { piProgressTask?.cancel(); piProgressTask = nil }
        }
        if state == .failed {
            deliverContext(PiJobTracker.failureChannel(reason: note ?? ""), followUp: true)
        }
        if state == .done || state == .stopped || state == .failed {
            appendPiJournal(PiJobTracker.journalLine(
                now: Date(), event: status, id: id,
                brief: piJobs.jobs[id]?.brief ?? ""
            ))
        }
    }

    /// Record a changed Pi note as silent [STATUS] context. No follow-up, so it
    /// is not spoken. Insert only between turns, so it cannot cut off a sentence.
    private func startPiProgressLoop(id: String) {
        piProgressTask?.cancel()
        lastPiProgressAt = Date()
        lastPiProgressNote = nil
        piProgressTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                guard !Task.isCancelled, let self, !self.closed,
                      self.piJobs.activeId == id, self.piJobs.hasActive else { return }
                let now = Date()
                let elapsed = now.timeIntervalSince(self.lastPiProgressAt)
                let note = self.piJobs.jobs[id]?.lastNote
                let changed = note != nil && note != self.lastPiProgressNote
                guard PiJobTracker.shouldSpeakProgress(elapsed: elapsed, changed: changed) else { continue }
                guard self.activeResponseId.isEmpty, !self.responseCreateRequested,
                      !self.audio.isPlaying, !self.userSpeechActive,
                      self.implicitTurnSince == nil else { continue }
                self.lastPiProgressAt = now
                self.lastPiProgressNote = note
                self.sendUserText(PiJobTracker.statusChannel(note: note ?? ""))
            }
        }
    }

    private func appendPiJournal(_ line: String) {
        PiJobTracker.appendJournal(directory: FileManager.default.temporaryDirectory, line: line)
    }

    func ingestUserText(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !closed else { return }
        sendUserText(trimmed)
        requestFollowUpIfIdle()
    }

    private func sendUserText(_ text: String) {
        send([
            "type": "conversation.item.create",
            "item": [
                "type": "message",
                "role": "user",
                "content": [["type": "input_text", "text": text]],
            ] as [String: Any],
        ])
    }

    private func requestFollowUpIfIdle() {
        if activeResponseId.isEmpty && !responseCreateRequested && !audio.isPlaying {
            sendResponseCreate()
        } else {
            responseRequestPending = true
        }
    }

    private func teardown(emitIdle: Bool) async {
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

    private func fail(_ message: String) {
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

    /// Receive off the MainActor. URLSession delivers on the session queue;
    /// awaiting receive() on MainActor deadlocks connecting forever.
    /// Audio deltas are decoded here so JSON/base64/resample do not hitch the panel.
    private static func pump(
        _ ws: URLSessionWebSocketTask,
        owner: LiveVoiceBackend?,
        pcm: PCMBridge,
        generation: UUID
    ) async {
        while !Task.isCancelled {
            let closed = await MainActor.run { owner?.closed != false || owner?.connectionGeneration != generation }
            if closed { break }
            do {
                let message = try await ws.receive()
                let text: String?
                switch message {
                case .string(let value): text = value
                case .data(let data): text = String(data: data, encoding: .utf8)
                @unknown default: text = nil
                }
                guard let text else { continue }
                if let delta = parseAudioDelta(text) {
                    let dest = await MainActor.run { () -> AVAudioFormat? in
                        guard let owner, owner.connectionGeneration == generation, !owner.closed else { return nil }
                        if owner.shouldDropAudio(responseId: delta.responseId) { return nil }
                        return owner.audio.playbackFormat
                    }
                    guard let dest, let buffer = pcm.playbackBuffer(base64: delta.b64, dest: dest) else { continue }
                    await MainActor.run {
                        owner?.playDecodedAudio(buffer, responseId: delta.responseId, generation: generation)
                    }
                } else {
                    await MainActor.run {
                        guard owner?.connectionGeneration == generation else { return }
                        owner?.handle(.string(text))
                    }
                }
            } catch {
                if Task.isCancelled { break }
                if Self.isHandshakeNotReady(error) {
                    let waiting = await MainActor.run {
                        owner?.connectionGeneration == generation
                            && owner?.closed == false
                            && owner?.connection == .awaitingSession
                    }
                    if waiting {
                        NSLog("[LiveVoice] receive before handshake, retrying: \(error.localizedDescription)")
                        try? await Task.sleep(nanoseconds: 50_000_000)
                        continue
                    }
                }
                NSLog("[LiveVoice] receive failed: \(error.localizedDescription)")
                await MainActor.run {
                    guard !Task.isCancelled, owner?.connectionGeneration == generation else { return }
                    owner?.fail("Can't reach the service")
                }
                break
            }
        }
    }

    /// `URLSessionWebSocketTask.receive()` can throw POSIX ENOTCONN if it
    /// runs in the window between `resume()` and the TCP handshake.
    private static func isHandshakeNotReady(_ error: Error) -> Bool {
        let ns = error as NSError
        if ns.domain == NSPOSIXErrorDomain && ns.code == Int(ENOTCONN) { return true }
        return ns.localizedDescription.localizedCaseInsensitiveContains("socket is not connected")
    }

    private struct AudioDelta {
        let responseId: String
        let b64: String
    }

    private static func parseAudioDelta(_ text: String) -> AudioDelta? {
        guard let data = text.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = json["type"] as? String,
              type == "response.audio.delta" || type == "response.output_audio.delta",
              let b64 = json["delta"] as? String, !b64.isEmpty
        else { return nil }
        let responseId: String
        if let id = json["response_id"] as? String {
            responseId = id
        } else if let response = json["response"] as? [String: Any], let id = response["id"] as? String {
            responseId = id
        } else {
            responseId = ""
        }
        return AudioDelta(responseId: responseId, b64: b64)
    }

    private func shouldDropAudio(responseId: String) -> Bool {
        !responseId.isEmpty && cancelledIds.contains(responseId)
    }

    private func playDecodedAudio(
        _ buffer: AVAudioPCMBuffer,
        responseId: String,
        generation: UUID
    ) {
        guard connectionGeneration == generation, !closed, !shouldDropAudio(responseId: responseId) else { return }
        audio.play(buffer)
        onState?(.agentSpeaking)
    }

    private func handle(_ message: URLSessionWebSocketTask.Message) {
        switch message {
        case .string(let text):
            handleMessage(text)
        case .data(let data):
            if let text = String(data: data, encoding: .utf8) {
                handleMessage(text)
            }
        @unknown default:
            break
        }
    }

    private func handleMessage(_ text: String) {
        guard let data = text.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = json["type"] as? String
        else { return }

        switch type {
        case "session.created":
            sendSessionUpdate()
            replayHistory()
            connection = .ready
            mic.setAccepting(true)
            handshakeTimeout?.cancel()
            handshakeTimeout = nil
            if !closed { onState?(.listening) }

        case "response.created":
            responseCreateRequested = false
            implicitTurnSince = nil
            lastResponseCreatedAt = Date()
            if let response = json["response"] as? [String: Any],
               let id = response["id"] as? String {
                activeResponseId = id
            }

        case "input_audio_buffer.speech_started":
            userSpeechActive = true
            onUserSpeechStarted?()
            // A response can be finished server-side while its audio is still
            // queued on this Mac. Stop that queue at confirmed user speech,
            // without waiting for the user's transcription to finish.
            if json["interrupt_response"] as? Bool == true, audio.isPlaying {
                audio.clearPlayback()
            }
            let responseActive = !activeResponseId.isEmpty
            if !closed,
               VoiceSpeechStartPolicy.shouldShowListening(
                   responseActive: responseActive,
                   playing: audio.isPlaying
               ) {
                onState?(.listening)
            }

        case "input_audio_buffer.turn_admitted":
            if let admission = parseTurnAdmission(json) {
                handleTurnAdmission(admission)
            }

        case "input_audio_buffer.speech_stopped":
            userSpeechActive = false
            // The turn is over on the server's side: show that the model is
            // working, not that the panel is idly listening. speech_started,
            // turn_ignored, audio or response.done all move it on.
            if !closed,
               VoiceSpeechStartPolicy.shouldShowListening(
                   responseActive: !activeResponseId.isEmpty,
                   playing: audio.isPlaying
               ) {
                onState?(.thinking)
            }

        case "conversation.item.input_audio_transcription.completed":
            let itemId = json["item_id"] as? String
            let transcript = (json["transcript"] as? String ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if transcript.isEmpty {
                // An empty final must not call onUserFinal — that would commit
                // an empty bubble.
                break
            }
            lastInputItemId = itemId
            // The server now answers this turn on its own; see VoiceContextHold.
            implicitTurnSince = Date()
            onUserFinal?(transcript, itemId)

        case "response.audio_transcript.delta", "response.output_audio_transcript.delta":
            if cancelledIds.contains(responseId(in: json)) { return }
            if let delta = json["delta"] as? String, !delta.isEmpty {
                pushAgentDelta(delta)
            }

        case "response.audio.delta", "response.output_audio.delta":
            let rid = responseId(in: json)
            if !rid.isEmpty, cancelledIds.contains(rid) { return }
            if let b64 = json["delta"] as? String,
               let buffer = pcm.playbackBuffer(base64: b64, dest: audio.playbackFormat) {
                audio.play(buffer)
            }
            onState?(.agentSpeaking)

        case "response.done":
            var cancelled = false
            if let response = json["response"] as? [String: Any],
               let id = response["id"] as? String {
                if !activeResponseId.isEmpty, id != activeResponseId { return }
                if id == activeResponseId { activeResponseId = "" }
                if response["status"] as? String == "cancelled" {
                    cancelled = true
                    rememberCancelled(id)
                    cancelToolWork()
                    audio.clearPlayback()
                }
            }
            // The reply is written back; held Pi context now follows it.
            releaseHeldContext()
            let ackCallId = handoffAckCallId
            handoffAckCallId = nil
            let acknowledge = VoiceToolFollowUp.shouldAcknowledgeHandoff(
                handoffCalled: ackCallId != nil, spokenText: agentText, cancelled: cancelled, muted: muted
            )
            if !serverToolNames.isEmpty {
                // A server-run tool whose output never arrived (the turn was
                // interrupted mid-call) must not leave the pill spinning.
                serverToolNames.removeAll()
                onToolsCancelled?()
            }
            finishAgentTurn()
            if !audio.isPlaying {
                if !closed { onState?(.listening) }
            }
            if responseRequestPending && !audio.isPlaying,
               VoiceToolFollowUp.shouldSend(
                   pendingTools: toolScope.pendingIds.count,
                   responseActive: !activeResponseId.isEmpty
               ) {
                responseRequestPending = false
                sendResponseCreate()
            }
            if acknowledge, let ackCallId, !responseCreateRequested, !responseRequestPending {
                NSLog("[LiveVoice] handoff %@ was silent; asking for an acknowledgement", ackCallId)
                requestFollowUp(callId: ackCallId)
            }

        case "response.function_call_arguments.done":
            if cancelledIds.contains(responseId(in: json)) { return }
            guard let name = json["name"] as? String,
                  let callId = json["call_id"] as? String
            else { break }
            let argsJson = json["arguments"] as? String ?? "{}"
            executeTool(name: name, argsJson: argsJson, callId: callId)

        case "conversation.item.created":
            // Tools the server ran inside the response arrive as the items they
            // created: a function_call when the call starts, its
            // function_call_output when it finishes. Show them; never run them.
            guard let item = json["item"] as? [String: Any],
                  let callId = item["call_id"] as? String else { break }
            if item["type"] as? String == "function_call", let name = item["name"] as? String {
                serverToolNames[callId] = name
                onToolActive?(name)
            } else if item["type"] as? String == "function_call_output",
                      let name = serverToolNames.removeValue(forKey: callId) {
                onToolDone?(name, item["output"] as? String ?? "")
            }

        case "error":
            // Transport close is fatal; server events like turn_ignored are not.
            if let error = json["error"] as? [String: Any], VoiceServerError.kind(error) == "turn_ignored" {
                releaseHeldContext()
                onTurnDropped?()
                if !closed, !audio.isPlaying, activeResponseId.isEmpty {
                    onState?(.listening)
                }
            } else if let error = json["error"] as? [String: Any] {
                if VoiceServerError.kind(error) == "response_cancel_not_active" { break }
                onRequestError?(error["message"] as? String ?? "The reply failed. Please retry.")
                responseCreateRequested = false
                responseRequestPending = false
            }

        default:
            break
        }
    }

    /// SessionController appends with `+=`, so only new text may be forwarded.
    /// If the server sends cumulative text, strip the prefix we already have.
    private func pushAgentDelta(_ incoming: String) {
        let chunk: String
        if incoming.hasPrefix(agentText), incoming.count >= agentText.count {
            chunk = String(incoming.dropFirst(agentText.count))
            agentText = incoming
        } else {
            chunk = incoming
            agentText += incoming
        }
        guard !chunk.isEmpty else { return }
        onAgentDelta?(chunk)
        onState?(.agentSpeaking)
    }

    private func finishAgentTurn() {
        let spoken = agentText.trimmingCharacters(in: .whitespacesAndNewlines)
        agentText = ""
        // Publish the finished transcript when the response completes. Audio
        // may still be playing for many seconds; the Pi chat should not wait
        // for the speaker queue to drain before showing what Luna is saying.
        if !spoken.isEmpty { onSpoken?(spoken) }
        onAgentDone?()
    }

    private func parseTurnAdmission(_ json: [String: Any]) -> VoiceTurnAdmission? {
        guard let id = json["admission_id"] as? String, !id.isEmpty else { return nil }
        let itemID = json["item_id"] as? String ?? ""
        let interrupts = json["interrupt_output"] as? Bool ?? true
        return VoiceTurnAdmission(id: id, itemID: itemID, interruptsOutput: interrupts)
    }

    private func handleTurnAdmission(_ admission: VoiceTurnAdmission) {
        guard VoiceTurnAdmissionPolicy.shouldApply(
            id: admission.id,
            seen: seenAdmissionIds,
            interruptsOutput: admission.interruptsOutput
        ) else { return }
        seenAdmissionIds.insert(admission.id)
        if seenAdmissionIds.count > 32, let oldest = seenAdmissionIds.first {
            seenAdmissionIds.remove(oldest)
        }
        cancelToolWork()
        rememberCancelled(activeResponseId)
        if audio.isPlaying {
            audio.clearPlayback()
        }
    }

    private func rememberCancelled(_ id: String) {
        guard !id.isEmpty else { return }
        cancelledIds.insert(id)
        if cancelledIds.count > 32, let oldest = cancelledIds.first {
            cancelledIds.remove(oldest)
        }
    }

    private func responseId(in json: [String: Any]) -> String {
        if let id = json["response_id"] as? String { return id }
        if let response = json["response"] as? [String: Any],
           let id = response["id"] as? String {
            return id
        }
        return ""
    }

    private func sendSessionUpdate() {
        // Persona only. The server's system prompt adds the research guidance
        // and the date. Nothing about tools needs to be re-sent from here.
        let tools = CommandLine.arguments.contains("--headless")
            ? headlessTalkerTools()
            : VoiceToolExecutor.shared.activeToolDefinitions()
        var sess: [String: Any] = [
            "type": "realtime",
            "instructions": instructions,
            "audio": ["output": ["voice": voice]],
        ]
        let thinker = ProcessInfo.processInfo.environment["VOICE_THINKER"]?.lowercased()
        if thinker == "luna" || thinker == "pi" {
            sess["thinker"] = thinker as Any
        }
        // Pi owns tools. Sending Luna's client tools would invite a think
        // follow-up this session must not start.
        if thinker != "pi", !tools.isEmpty {
            sess["tools"] = tools
            sess["tool_choice"] = "auto"
        }
        send([
            "type": "session.update",
            "session": sess,
        ])
    }

    private func headlessTalkerTools() -> [[String: Any]] {
        // Agent talks. One handoff sends real work to the open Pi session.
        // Progress and the answer arrive as [STATUS] and [FINAL], not as tools
        // the model polls. stop_thinking cancels the current job and leaves
        // the voice call up; `/voice stop` is what ends the call.
        return [Self.spawnThinkingTool, Self.stopThinkingTool]
    }

    private static let spawnThinkingTool: [String: Any] = [
        "type": "function",
        "name": "spawn_thinking",
        "description": "Hand real work to the open Pi session: files, PDFs, resumes, the shell, web research, code changes, what is on screen, controlling the computer (click, type, fill forms, navigate), or anything you are not sure about. You cannot see the screen or click yourself. Answer casual chat yourself when you already have the context; do not call this for that. A call while Pi is working steers it: the latest brief replaces the current task, so a correction does not need stop_thinking. Call again only when the task really changes, not when the user just confirms, repeats, says continue, rewords the same task, or asks how it is going (answer that from [STATUS]). Then say something like \"Okay, I've redirected Pi to that instead.\" Never say it is queued or runs after that or next. Include paths the user gave. Ask for a missing save destination. Returns immediately. Say one short acknowledgement in this same turn, then wait. Progress arrives as [STATUS] and the answer as [FINAL]. Do not claim you already did the work.",
        "parameters": [
            "type": "object",
            "properties": [
                "brief": [
                    "type": "string",
                    "description": "What Pi should do or change in the current task.",
                ],
            ],
            "required": ["brief"],
        ] as [String: Any],
    ]

    /// Spoken cancel for the current handoff. Fire-and-forget, like the
    /// handoff itself, so the voice line can say what stopped. `/voice stop`
    /// still ends the call; this does not.
    private static let stopThinkingTool: [String: Any] = [
        "type": "function",
        "name": "stop_thinking",
        "description": "Stop the Pi task that is running now, when the user explicitly asks you to stop, cancel, or says never mind. Does not end the voice conversation. A correction or follow-up is spawn_thinking, not this. Returns immediately. Say what you stopped.",
        "parameters": [
            "type": "object",
            "properties": [:] as [String: Any],
        ] as [String: Any],
    ]

    private static func json(_ object: [String: Any]) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: object),
              let text = String(data: data, encoding: .utf8), !text.isEmpty else { return "{}" }
        return text
    }

    private static func spawnBrief(_ argsJson: String) -> String {
        guard let data = argsJson.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let brief = object["brief"] as? String
        else { return "" }
        return brief.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Status and result answers are the point of the call — unlike the
    /// fire-and-forget hand-offs, Luna must speak them, so ask the model to
    /// continue once the output lands. Mirrors the supervised-tool path minus
    /// the scope bookkeeping these synchronous answers do not need.
    private func requestFollowUp(callId: String) {
        requestResponse()
        if toolScope.pendingIds.isEmpty {
            armFollowUpWatchdog(callId: callId, generation: toolScope.generation)
        }
    }

    /// Inject the dated startup pack into the live conversation. No response is
    /// requested, so the pack is context and is not spoken.
    private func replayHistory() {
        var replayable: [(role: String, text: String)] = []
        for m in historyMessages {
            let text = m.text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { continue }
            if m.role == "user" || m.role == "assistant" {
                replayable.append((m.role, text))
            } else if m.role == "tool" {
                let name = (m.name ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                let shown = text.count > 500 ? String(text.prefix(497)) + "..." : text
                replayable.append(("assistant", "[Earlier I used \(name.isEmpty ? "tool" : name)] \(shown)"))
            }
        }
        for m in replayable {
            let type = m.role == "assistant" ? "output_text" : "input_text"
            send([
                "type": "conversation.item.create",
                "item": [
                    "type": "message",
                    "role": m.role,
                    "content": [["type": type, "text": m.text]],
                ] as [String: Any],
            ])
        }
        if !replayable.isEmpty {
            NSLog("[LiveVoice] replayed %d saved message(s)", replayable.count)
        }
    }

    private func cancelToolWork() {
        if !closed {
            for callId in toolScope.pendingIds {
                sendToolOutput(callId: callId, output: "This tool was cancelled because the conversation moved on. Its result is unavailable.")
            }
        }
        toolScope.cancel()
        serverToolNames.removeAll()
        handoffAckCallId = nil
        onToolsCancelled?()
        responseRequestPending = false
    }

    /// Run a tool the server forwarded to the client (`spawn_thinking` /
    /// `stop_thinking`), post its output, and leave the voice line free.
    /// Progress and the final answer are pushed later on their channels.
    /// A handoff while a job is active steers it, and the ack says so. A
    /// handoff while the mic is muted still returns an ack here; the
    /// extension drops it and reports `dropped`, which stays silent.
    private func executeTool(name: String, argsJson: String, callId: String) {
        guard !closed, seenToolCalls.insert(callId).inserted else { return }
        if name == "spawn_thinking" {
            let brief = Self.spawnBrief(argsJson)
            // The same task again within seconds: keep Pi's current work.
            if let current = piJobs.activeRepeat(of: brief) {
                sendToolOutput(callId: callId, output: Self.json(PiJobTracker.repeatAck(id: current)))
                handoffAckCallId = callId
                appendPiJournal(PiJobTracker.journalLine(now: Date(), event: "repeat", id: current, brief: brief))
                return
            }
            let id = UUID().uuidString
            // Read before ask(): the new id becomes active there.
            let steering = piJobs.hasActive
            piJobs.ask(id: id, brief: brief)
            startPiProgressLoop(id: id)
            sendToolOutput(callId: callId, output: Self.json(PiJobTracker.handoffAck(id: id, steering: steering)))
            handoffAckCallId = callId
            appendPiJournal(PiJobTracker.journalLine(now: Date(), event: steering ? "steered" : "queued", id: id, brief: brief))
            if !brief.isEmpty { onSpawnThinking?(id, brief) }
            return
        }
        if name == "stop_thinking" {
            handoffAckCallId = callId
            if piJobs.hasActive {
                sendToolOutput(callId: callId, output: "{\"status\":\"stopping\"}")
                onStopThinking?()
            } else {
                sendToolOutput(callId: callId, output: "{\"status\":\"idle\"}")
            }
            return
        }
        let generation = toolScope.generation
        NSLog("[LiveVoice] tool call: %@ id=%@", name, callId)
        onToolActive?(name)
        let task = Task {
            let result = await VoiceToolExecutor.shared.run(name: name, argsJson: argsJson)
            guard !Task.isCancelled, !self.closed, self.toolScope.generation == generation else {
                NSLog("[LiveVoice] tool %@ dropping result pre-send: cancelled=%@ closed=%@ genMatch=%@",
                      name, "\(Task.isCancelled)", "\(!self.closed)", "\(self.toolScope.generation == generation)")
                return
            }
            NSLog("[LiveVoice] tool %@ done: output=%d chars", name, result.output.count)
            self.sendToolOutput(callId: callId, output: result.output)
            guard !Task.isCancelled, !self.closed, self.toolScope.generation == generation else {
                NSLog("[LiveVoice] tool %@ dropping follow-up post-send: cancelled=%@ closed=%@ genMatch=%@",
                      name, "\(Task.isCancelled)", "\(!self.closed)", "\(self.toolScope.generation == generation)")
                return
            }
            self.toolScope.finish(callId, generation: generation)
            self.onToolDone?(name, result.output)
            self.requestResponse()
            if self.toolScope.pendingIds.isEmpty {
                self.armFollowUpWatchdog(callId: callId, generation: generation)
            }
        }
        toolScope.insert(task, id: callId)
    }

    private func sendToolOutput(callId: String, output: String) {
        send([
            "type": "conversation.item.create",
            "item": [
                "type": "function_call_output",
                "call_id": callId,
                "output": output,
            ] as [String: Any],
        ])
    }


    /// Ask the model to continue after a tool result.
    ///
    /// A tool that returns faster than the in-flight response finishes (a
    /// bridge miss answers immediately) would otherwise race it, and the server
    /// answers an overlapping create with
    /// `conversation_already_has_active_response`. Nothing retried that, so the
    /// turn died after the spoken acknowledgement and the tool result was never
    /// used — which also stops any fallback chain at its first rung. Defer
    /// instead, and flush on `response.done`.
    ///
    /// Parallel tools in one response must also wait for each other. Sending a
    /// follow-up after the first of several `web_search` results makes the
    /// model answer twice with the same content once the rest arrive.
    private func requestResponse() {
        guard toolScope.pendingIds.isEmpty else {
            NSLog("[LiveVoice] follow-up waiting for %d remaining tool(s)", toolScope.pendingIds.count)
            return
        }
        guard activeResponseId.isEmpty else {
            NSLog("[LiveVoice] follow-up deferred (response %@ active)", activeResponseId)
            responseRequestPending = true
            return
        }
        if responseCreateRequested {
            NSLog("[LiveVoice] follow-up deferred (create already requested)")
            responseRequestPending = true
            return
        }
        NSLog("[LiveVoice] follow-up: sending response.create now")
        sendResponseCreate()
    }

    private func sendResponseCreate() {
        guard !closed, !responseCreateRequested else {
            NSLog("[LiveVoice] sendResponseCreate skipped: closed=%@ alreadyRequested=%@",
                  "\(!closed)", "\(responseCreateRequested)")
            return
        }
        responseCreateRequested = true
        NSLog("[LiveVoice] sending response.create (follow-up)")
        send([
            "type": "response.create",
            "response": [:] as [String: Any],
        ])
    }

    /// Retry a tool follow-up once if the server never starts a response.
    /// Covers silent drops between tool completion and response.created
    /// (e.g. a create lost in an echo-cancel race). Stands down on any newer
    /// response, a superseding generation, or close. A duplicate create that
    /// arrives after the server already started one is rejected by the server
    /// and ignored by the client, so the retry is safe.
    private func armFollowUpWatchdog(callId: String, generation: UUID) {
        let requestedAt = Date()
        DispatchQueue.main.asyncAfter(deadline: .now() + 10) { [weak self] in
            guard let self, !self.closed, self.toolScope.generation == generation else {
                NSLog("[LiveVoice] follow-up watchdog %@: stood down (superseded)", callId)
                return
            }
            guard self.lastResponseCreatedAt < requestedAt else {
                NSLog("[LiveVoice] follow-up watchdog %@: server responded, stood down", callId)
                return
            }
            NSLog("[LiveVoice] follow-up watchdog %@: no response in 10s, resending response.create", callId)
            self.responseCreateRequested = false
            self.responseRequestPending = false
            self.sendResponseCreate()
        }
    }

    private func send(_ object: [String: Any]) {
        socket.send(object)
    }
}
