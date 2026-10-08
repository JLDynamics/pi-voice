import Foundation

@main
struct RuntimeTests {
    @MainActor
    static func main() async throws {
        let standardVoice = URL(string: "ws://127.0.0.1:8766/v1/realtime")!
        assert(LocalServiceStarter.manages(voice: standardVoice))
        assert(!LocalServiceStarter.manages(voice: URL(string: "wss://example.com/v1/realtime")!))
        assert(!LocalServiceStarter.manages(voice: URL(string: "ws://127.0.0.1:8866/v1/realtime")!))

        // A running service is reused only while it runs this checkout's current code.
        let root = "/Users/me/chatbot"
        let voiceHealth: [String: Any] = ["ready": true, "server_tools": true, "fingerprint": "abc", "stale": false, "source": root]
        assert(LocalServiceStarter.voiceStatus(code: 200, json: voiceHealth, root: root) == .ready)
        assert(LocalServiceStarter.voiceStatus(code: 200, json: voiceHealth.merging(["ready": false]) { $1 }, root: root) == .starting)
        assert(LocalServiceStarter.voiceStatus(code: 200, json: voiceHealth.merging(["ready": false, "server_tools": false]) { $1 }, root: root) == .starting)
        assert(LocalServiceStarter.voiceStatus(code: 200, json: voiceHealth.merging(["stale": true]) { $1 }, root: root) == .stale)
        assert(LocalServiceStarter.voiceStatus(code: 200, json: voiceHealth.merging(["server_tools": false]) { $1 }, root: root) == .ready)
        assert(LocalServiceStarter.voiceStatus(code: 200, json: voiceHealth.merging(["source": "/Users/me/chatbot-refactor"]) { $1 }, root: root) == .stale)
        assert(LocalServiceStarter.voiceStatus(code: 200, json: voiceHealth.merging(["source": root + "/"]) { $1 }, root: root) == .ready)
        // Pre-fingerprint servers answer without the contract; they cannot be current.
        assert(LocalServiceStarter.voiceStatus(code: 200, json: ["status": "ok", "ready": true], root: root) == .stale)
        assert(LocalServiceStarter.voiceStatus(code: 404, json: nil, root: root) == .stale)
        let message = try JSONDecoder().decode(
            ChatMessage.self,
            from: Data(#"{"role":"tool","text":"done","name":"bash"}"#.utf8)
        )
        assert(message.role == "tool" && message.text == "done" && message.name == "bash")
        let tracker = PlaybackTracker()
        let old = tracker.enqueue()
        tracker.clear()
        let current = tracker.enqueue()
        assert(!tracker.complete(old))
        assert(tracker.isAudible, "An abandoned buffer must not drain new playback")
        let now = Date()
        assert(tracker.complete(current, now: now))
        assert(!tracker.isAudible)
        assert(tracker.needsEchoGuard(now: now.addingTimeInterval(0.5)))
        assert(!tracker.needsEchoGuard(now: now.addingTimeInterval(1)))
        assert(tracker.needsFirstReplyGuard(now: now.addingTimeInterval(0.5)))
        assert(!tracker.needsFirstReplyGuard(now: now.addingTimeInterval(1)))
        let later = tracker.enqueue()
        assert(!tracker.needsFirstReplyGuard(now: now.addingTimeInterval(1)), "later replies keep normal barge-in")
        assert(tracker.complete(later))
        tracker.resetForSession()
        let newFirst = tracker.enqueue()
        assert(tracker.needsFirstReplyGuard(), "a new call protects its first reply")
        assert(tracker.complete(newFirst))

        assert(!VoiceToolFollowUp.shouldSend(pendingTools: 3, responseActive: false))
        assert(!VoiceToolFollowUp.shouldSend(pendingTools: 0, responseActive: true))
        assert(!VoiceToolFollowUp.shouldSend(pendingTools: 1, responseActive: true))
        assert(VoiceToolFollowUp.shouldSend(pendingTools: 0, responseActive: false))
        // A handoff the model made without a word must still be acknowledged
        // (live runs: text='' with spawn_thinking every time), once, and never
        // on top of a reply that already spoke or was cut off.
        assert(VoiceToolFollowUp.shouldAcknowledgeHandoff(handoffCalled: true, spokenText: " \n", cancelled: false))
        assert(!VoiceToolFollowUp.shouldAcknowledgeHandoff(handoffCalled: true, spokenText: "On it.", cancelled: false))
        assert(!VoiceToolFollowUp.shouldAcknowledgeHandoff(handoffCalled: true, spokenText: "", cancelled: true))
        assert(!VoiceToolFollowUp.shouldAcknowledgeHandoff(handoffCalled: false, spokenText: "", cancelled: false))
        assert(!VoiceToolFollowUp.shouldAcknowledgeHandoff(handoffCalled: true, spokenText: "", cancelled: false, muted: true),
               "a muted handoff is dropped by the extension; do not promise it")
        // Pi's [FINAL] waits while the server answers a transcribed turn it has
        // not announced yet, but never forever.
        // turn_ignored arrives as error.type with a null code; it is not a failure.
        assert(VoiceServerError.kind(["type": "turn_ignored", "code": NSNull(), "message": "Turn ignored (no_text)"]) == "turn_ignored")
        assert(VoiceServerError.kind(["type": "invalid_request_error", "code": "response_cancel_not_active"]) == "response_cancel_not_active")
        assert(VoiceServerError.kind(["message": "x"]) == "")
        let heldAt = Date()
        assert(!VoiceContextHold.shouldHold(implicitTurnSince: nil, now: heldAt))
        assert(VoiceContextHold.shouldHold(implicitTurnSince: heldAt, now: heldAt.addingTimeInterval(3)))
        assert(!VoiceContextHold.shouldHold(implicitTurnSince: heldAt, now: heldAt.addingTimeInterval(VoiceContextHold.maxHold)))

        let scope = VoiceWorkScope()
        let oldGeneration = scope.generation
        let task = Task<Void, Never> { try? await Task.sleep(nanoseconds: 10_000_000_000) }
        scope.insert(task, id: "old")
        scope.cancel()
        assert(task.isCancelled && oldGeneration != scope.generation)
        let newTask = Task {}
        scope.insert(newTask, id: "old")
        scope.finish("old", generation: oldGeneration)
        assert(scope.contains("old"), "A stale completion must not remove new work")
        scope.cancel()

        // No client-side tools remain: the client only publishes the
        // server-run definitions. Luna no longer sees the screen.
        let names = VoiceToolExecutor.shared.activeToolDefinitions().compactMap { $0["name"] as? String }
        assert(Set(names).count == names.count, "Tool names must be unique")
        assert(names.contains("bash"), "This branch publishes bash for public research")
        assert(!names.contains("web_search"))
        let bash = VoiceToolExecutor.shared.activeToolDefinitions().first { $0["name"] as? String == "bash" }
        let bashDesc = bash?["description"] as? String ?? ""
        assert(bashDesc.contains("when:1d"), "bash tool must tell the model to date-filter news")
        assert(bashDesc.contains("Wikipedia"), "office-holder facts should fetch Wikipedia, not a news feed")
        assert(bashDesc.contains("voice model") || bashDesc.contains("Research the voice model"),
               "bash is the voice model's research tool")
        assert(!names.contains("code_agent"), "The coding agent was removed; Claude Code covers that job")
        // Removed with the sidecar that served them; Pi owns web reading and memory.
        assert(!names.contains("read_page") && !names.contains("web_fetch") && !names.contains("read_article"),
               "Page-reading tools went with the sidecar")
        assert(!names.contains("remember") && !names.contains("forget") && !names.contains("search_chat_history"),
               "Memory tools went with the sidecar")
        assert(!names.contains("screenshot"), "The screenshot tool was removed")
        assert(VoiceToolExecutor.serverSideTools == ["bash"], "bash is the only tool the server runs")
        for name in names where VoiceToolExecutor.serverSideTools.contains(name) {
            assert(["bash", "read_page", "remember", "forget", "search_chat_history"].contains(name))
        }
        let unavailable = await VoiceToolExecutor.shared.run(name: "web_search", argsJson: "{\"query\":\"x\"}")
        assert(unavailable.output.contains("runs on the server"), "Research tools never execute in the app")

        testMicCapture()
        testSpeechStartPolicy()
        testTranscript()
        testIdleChromeKeepsMute()
        testHeadlessBridge()
        await testSessionConversationHistory()
        await testStartFailureReachesBridge()
        testPiJobTracker()
        assert(!PiJobTracker.shouldSpeakProgress(elapsed: 7, changed: true))
        assert(PiJobTracker.shouldSpeakProgress(elapsed: 8, changed: true))
        assert(PiJobTracker.shouldSpeakProgress(elapsed: 20, changed: true))
        assert(!PiJobTracker.shouldSpeakProgress(elapsed: 19, changed: false))
        assert(!PiJobTracker.shouldSpeakProgress(elapsed: 20, changed: false))
        assert(!PiJobTracker.shouldSpeakProgress(elapsed: 60, changed: false))
        let status = PiJobTracker.statusChannel(note: "Pi is reading files")
        assert(status.hasPrefix("[STATUS] Pi is reading files."))
        assert(status.contains("do not speak it"))
        assert(status.contains("This is not the user."))
        assert(PiJobTracker.statusChannel(note: "  ").contains("Pi is still working"))
        assert(PiJobTracker.failureChannel(reason: "no answer").contains("The task failed: no answer"))
        let done = PiJobTracker.finalChannel(excerpt: "It compiled.", outcome: .done)
        assert(done.hasPrefix("[FINAL] Pi finished."))
        assert(done.contains("It compiled."))
        let partial = PiJobTracker.finalChannel(excerpt: "halfway", outcome: .stopped)
        assert(partial.contains("incomplete task (stopped)"))
        assert(!partial.contains("pi_results"))
        print("Native runtime checks passed: playback, cancellation, tool definitions, transcript revisions, headless bridge, pi jobs, conversation history")
    }

    static func pcm16(_ samples: [Int16]) -> [UInt8] {
        var bytes = [UInt8]()
        bytes.reserveCapacity(samples.count * 2)
        for sample in samples {
            let raw = UInt16(bitPattern: sample)
            bytes.append(UInt8(raw & 0xff))
            bytes.append(UInt8((raw >> 8) & 0xff))
        }
        return bytes
    }

    static func sinePCM(hz: Double, amplitude: Int16, count: Int, sampleRate: Double = 16_000) -> [UInt8] {
        let step = 2 * Double.pi * hz / sampleRate
        let samples: [Int16] = (0..<count).map { i in
            Int16((sin(Double(i) * step) * Double(amplitude)).rounded())
        }
        return pcm16(samples)
    }

    static func testMicCapture() {
        let capture = MicCapture()
        let gen = UUID()
        capture.arm(generation: gen)
        capture.setAccepting(true)
        let speech = sinePCM(hz: 500, amplitude: 4_000, count: 640)
        var sent: [UInt8]?
        for _ in 0..<4 {
            if let chunk = capture.ingest(speech, generation: gen) { sent = chunk }
        }
        assert(sent != nil, "Accepting capture must emit a batched chunk")
        capture.setMuted(true)
        assert(capture.ingest(speech, generation: gen) == nil, "Mute must drop the next ingest without waiting")
        let gen2 = UUID()
        capture.arm(generation: gen2)
        capture.setAccepting(true)
        assert(capture.ingest(speech, generation: gen2) == nil, "arm must not unmute")
        capture.setMuted(false)
        var unmuted: [UInt8]?
        for _ in 0..<4 {
            if let chunk = capture.ingest(speech, generation: gen2) { unmuted = chunk }
        }
        assert(unmuted != nil, "Unmute after arm must emit again")
        capture.disarm()
        assert(capture.ingest(speech, generation: gen) == nil, "Disarmed capture must drop audio")
    }

    static func testSpeechStartPolicy() {
        assert(
            !VoiceSpeechStartPolicy.shouldShowListening(responseActive: true, playing: false),
            "TTS chunk gaps during a response must not look like Listening"
        )
        assert(
            !VoiceSpeechStartPolicy.shouldShowListening(responseActive: false, playing: true)
        )
        assert(
            VoiceSpeechStartPolicy.shouldShowListening(responseActive: false, playing: false)
        )
        assert(
            VoiceTurnAdmissionPolicy.shouldApply(id: "adm_1", seen: [], interruptsOutput: true)
        )
        assert(
            !VoiceTurnAdmissionPolicy.shouldApply(id: "adm_1", seen: ["adm_1"], interruptsOutput: true)
        )
        assert(
            !VoiceTurnAdmissionPolicy.shouldApply(id: "adm_1", seen: [], interruptsOutput: false)
        )
    }

    @MainActor
    static func testIdleChromeKeepsMute() {
        let backend = MockVoiceBackend()
        let session = SessionController(backend: backend)
        backend.onState?(.listening)
        session.setMuted(true)
        assert(session.isMuted, "Headless mute must set session mute while live")
        session.requestEnd()
        assert(session.isMuted, "going idle must not unmute")
        assert(session.state == .idle)
    }

    @MainActor
    static func testTranscript() {
        let backend = MockVoiceBackend()
        let session = SessionController(backend: backend)
        backend.onState?(.listening)
        session.setMuted(true)
        assert(session.isMuted, "Headless mute must set session mute while live")
        session.setMuted(false)
        assert(!session.isMuted)
        backend.onUserSpeechStarted?()
        backend.onUserFinal?("I want to explain", "turn-one")
        // A final is held until the turn settles. Pausing mid-sentence finalizes
        // each revision, and painting every one rewrote the bubble while the
        // user was still talking.
        assert(session.turns.isEmpty, "A finalized turn waits for the turn to settle")
        assert(session.userSpeaking, "The indicator stays up while the turn is held")
        backend.onAgentDelta?("Go ahead, I am listening carefully to the microphone problem.")
        assert(!session.userSpeaking, "Painting the words lowers the indicator")
        let original = session.turns[0].id
        assert(session.turns[0].text == "I want to explain")
        backend.onAgentDone?()
        backend.onUserFinal?("I want to explain the microphone problem", "turn-one")
        backend.onTurnDropped?()
        assert(session.turns.count == 2 && session.turns[0].id == original)
        backend.onUserFinal?("Now read this other page", "turn-two")
        backend.onTurnDropped?()
        assert(session.turns.count == 3, "A distinct utterance must not overwrite earlier speech")

        // Pausing repeatedly inside one turn must not append the turn to itself.
        // Every revision's final is decoded from all of that turn's audio, and
        // STT revises words between passes ("thing" -> "things over there").
        // That defeated the merge heuristic, which then appended one more copy
        // of the whole turn on each pause until the bubble was unreadable.
        let repeatBackend = MockVoiceBackend()
        let repeats = SessionController(backend: repeatBackend)
        let head = "i'm sorry i have to interrupt you i need to test this feature"
        repeatBackend.onUserSpeechStarted?()
        repeatBackend.onUserFinal?(head, "turn-pause")
        repeatBackend.onUserFinal?("\(head) and see whether it's good it's still the other thing", "turn-pause")
        repeatBackend.onUserFinal?("\(head) and see whether it's good it's still are the other things over there", "turn-pause")
        assert(repeats.turns.isEmpty, "Revisions must not paint while the turn is still open")
        assert(repeats.userSpeaking, "The indicator covers the pauses")
        repeatBackend.onTurnDropped?()
        let shown = repeats.turns[0].text
        assert(repeats.turns.count == 1, "Pausing inside one turn must not open more bubbles")
        assert(
            shown == "\(head) and see whether it's good it's still are the other things over there",
            "Each revision must replace the bubble; got: \(shown)"
        )
        assert(
            shown.components(separatedBy: "i'm sorry").count - 1 == 1,
            "The turn must appear exactly once, not once per pause"
        )

        let pauseBackend = MockVoiceBackend()
        let pauseSession = SessionController(backend: pauseBackend)
        pauseBackend.onUserFinal?("yeah i still need to finish", "turn-a")
        pauseBackend.onUserFinal?("a lot of work to do", "turn-b")
        pauseBackend.onTurnDropped?()
        assert(pauseSession.turns.count == 1, "A paused continuation must stay one bubble")
        assert(pauseSession.turns[0].text.contains("yeah i still need to finish"))
        assert(pauseSession.turns[0].text.contains("a lot of work to do"))

        let fillerBackend = MockVoiceBackend()
        let fillerSession = SessionController(backend: fillerBackend)
        fillerBackend.onUserFinal?("i still need to finish", "turn-fill-a")
        fillerBackend.onAgentDelta?("...")
        fillerBackend.onAgentDone?()
        fillerBackend.onUserFinal?("a lot of work today", "turn-fill-b")
        fillerBackend.onTurnDropped?()
        assert(fillerSession.turns.count == 2, "Keep the tiny agent filler row")
        assert(fillerSession.turns[0].speaker == .you)
        assert(fillerSession.turns[0].text.contains("i still need to finish"))
        assert(fillerSession.turns[0].text.contains("a lot of work today"))
        assert(fillerSession.turns[1].text == "...")

        let restateBackend = MockVoiceBackend()
        let restateSession = SessionController(backend: restateBackend)
        restateBackend.onUserFinal?("i still need to finish a lot of work to do", "turn-restate")
        restateBackend.onUserFinal?("i still need to finish a lot of work today", "turn-restate")
        restateBackend.onTurnDropped?()
        assert(restateSession.turns.count == 1)
        assert(restateSession.turns[0].text == "i still need to finish a lot of work today")

        let splitBackend = MockVoiceBackend()
        let splitSession = SessionController(backend: splitBackend)
        splitBackend.onUserFinal?("yeah i still need to finish", "turn-split-a")
        splitBackend.onTurnDropped?()
        splitBackend.onUserFinal?("a lot of work to do", "turn-split-b")
        splitBackend.onTurnDropped?()
        assert(splitSession.turns.count == 1, "A paused continuation must stay one bubble")
        assert(splitSession.turns[0].text.contains("yeah i still need to finish"))
        assert(splitSession.turns[0].text.contains("a lot of work to do"))

        // The talking indicator is the only speaking feedback, so it must never
        // strand: every way a turn can end has to lower it.
        let barsBackend = MockVoiceBackend()
        let bars = SessionController(backend: barsBackend)
        assert(!bars.userSpeaking, "Idle shows no indicator")
        barsBackend.onUserSpeechStarted?()
        assert(bars.userSpeaking, "Speech start raises the indicator")
        barsBackend.onTurnDropped?()
        assert(!bars.userSpeaking, "A turn the server drops lowers the indicator")
        barsBackend.onUserSpeechStarted?()
        barsBackend.onUserFinal?("what is left on my list", "turn-bars")
        assert(bars.userSpeaking, "A held final keeps the indicator up through a pause")
        assert(bars.turns.isEmpty, "and paints nothing yet")
        barsBackend.onAgentDelta?("Two things.")
        assert(!bars.userSpeaking, "Painting the words lowers the indicator")
        assert(bars.turns.first?.text == "what is left on my list")
        barsBackend.onUserSpeechStarted?()
        barsBackend.onUserFinal?("one more thing", "turn-bars-2")
        assert(bars.userSpeaking)
        bars.requestEnd()
        assert(!bars.userSpeaking, "Stopping lowers the indicator")
        assert(
            bars.turns.contains { $0.text.contains("one more thing") },
            "Stopping must paint held words rather than losing them"
        )

        let junkBackend = MockVoiceBackend()
        let junkSession = SessionController(backend: junkBackend)
        // The server drops filler before it reaches the client, so a dropped
        // turn must leave no bubble and no indicator behind.
        junkBackend.onUserSpeechStarted?()
        junkBackend.onTurnDropped?()
        assert(junkSession.turns.isEmpty, "A dropped turn leaves no bubble")
        assert(!junkSession.userSpeaking, "A dropped turn lowers the indicator")
    }

    @MainActor
    static func testHeadlessBridge() {
        let backend = MockVoiceBackend()
        let session = SessionController(backend: backend)
        backend.onState?(.listening)

        let bridge = HeadlessBridge()
        var emitted = [[String: Any]]()
        bridge.emitSink = { obj in
            emitted.append(obj)
        }
        bridge.attach(session: session, listenToStdin: false)

        // Ready event on listening state transition
        backend.onState?(.listening)
        assert(emitted.contains { ($0["type"] as? String) == "ready" }, "Bridge emits ready when backend listens")

        // Wire contract (out): heard
        emitted.removeAll()
        backend.onUserFinal?("hello pi", "item-1")
        assert(emitted.count == 1, "heard event emitted")
        assert((emitted[0]["type"] as? String) == "heard")
        assert((emitted[0]["text"] as? String) == "hello pi")
        assert((emitted[0]["item_id"] as? String) == "item-1")

        // Wire contract (out): spoken
        emitted.removeAll()
        backend.onAgentDelta?("hello")
        assert(emitted.count == 1, "spoken delta emitted before completion")
        assert((emitted[0]["type"] as? String) == "spoken_delta")
        assert((emitted[0]["text"] as? String) == "hello")
        let spokenID = emitted[0]["item_id"] as? String
        assert(!(spokenID ?? "").isEmpty)
        emitted.removeAll()
        backend.onSpoken?("hello human")
        assert(emitted.count == 1, "spoken event emitted")
        assert((emitted[0]["type"] as? String) == "spoken")
        assert((emitted[0]["text"] as? String) == "hello human")
        assert((emitted[0]["item_id"] as? String) == spokenID)

        // Wire contract (out): work
        emitted.removeAll()
        backend.onSpawnThinking?("call-1", "check system status")
        assert(emitted.count == 1, "work event emitted")
        assert((emitted[0]["type"] as? String) == "work")
        assert((emitted[0]["id"] as? String) == "call-1")
        assert((emitted[0]["brief"] as? String) == "check system status")

        // Wire contract (in): mute
        assert(!session.isMuted)
        bridge.handle(line: #"{"type":"mute","muted":true}"#)
        assert(session.isMuted, "Mute line mutes session")
        bridge.handle(line: #"{"type":"mute","muted":false}"#)
        assert(!session.isMuted, "Unmute line unmutes session")

        // Wire contract (in): interrupt
        let priorInterrupts = backend.interruptCount
        bridge.handle(line: #"{"type":"interrupt"}"#)
        assert(backend.interruptCount > priorInterrupts, "Interrupt line interrupts backend")

        // Wire contract (in): user
        let priorUserInterrupts = backend.interruptCount
        bridge.handle(line: #"{"type":"user","text":"write a poem"}"#)
        assert(backend.interruptCount > priorUserInterrupts, "User line interrupts session")
        assert(backend.ingestedUserText.contains("write a poem"), "User line ingests text")

        // Wire contract (in): result
        bridge.handle(line: #"{"type":"result","id":"call-1","speak":"here is the poem","full":"here is the complete poem"}"#)
        assert(backend.postedResults.contains { $0.id == "call-1" && $0.speak == "here is the poem" && $0.full == "here is the complete poem" }, "Result line keeps both the spoken preview and complete result")
        let fullReport = String(repeating: "long detail ", count: 1000)
        let reportLine = try! JSONSerialization.data(withJSONObject: [
            "type": "result", "id": "call-2", "speak": "summary", "full": fullReport
        ])
        bridge.handle(line: reportLine)
        assert(backend.postedResults.last?.full == fullReport, "bridge preserves a result longer than 6000 characters")

        // Wire contract (in): job_update
        bridge.handle(line: #"{"type":"job_update","id":"call-1","status":"working","note":"using bash"}"#)
        assert(backend.piJobUpdates.count == 1, "job_update line forwards phase")
        assert(backend.piJobUpdates[0].id == "call-1")
        assert(backend.piJobUpdates[0].status == "working")
        assert(backend.piJobUpdates[0].note == "using bash")
        // Missing id or status is ignored, like any malformed line.
        bridge.handle(line: #"{"type":"job_update","status":"working"}"#)
        bridge.handle(line: #"{"type":"job_update","id":"call-1"}"#)
        assert(backend.piJobUpdates.count == 1, "job_update without id/status is ignored")

        // Malformed lines and unknown types ignored safely
        bridge.handle(line: "not json at all")
        bridge.handle(line: "{broken json")
        bridge.handle(line: "")
        bridge.handle(line: #"{"type":"future_unknown_command","data":123}"#)
        bridge.handle(line: #"{"no_type_field":true}"#)

        // Wire contract (out): speech_started
        emitted.removeAll()
        backend.onUserSpeechStarted?()
        assert(emitted.count == 1, "speech_started event emitted")
        assert((emitted[0]["type"] as? String) == "speech_started")

        // Wire contract (out): stop_work
        emitted.removeAll()
        backend.onStopThinking?()
        assert(emitted.count == 1, "stop_work event emitted")
        assert((emitted[0]["type"] as? String) == "stop_work")

        // Wire contract (out): error
        emitted.removeAll()
        backend.onState?(.failed("socket connection lost"))
        assert(emitted.count == 1, "error event emitted")
        assert((emitted[0]["type"] as? String) == "error")
        assert((emitted[0]["message"] as? String) == "socket connection lost")

        // Reattaching bridge does not duplicate event emissions
        bridge.attach(session: session, listenToStdin: false)
        emitted.removeAll()
        backend.onUserFinal?("hello again", "item-reattach")
        assert(emitted.count == 1, "reattached bridge must emit exactly one heard event")
        assert((emitted[0]["type"] as? String) == "heard")
    }

    @MainActor
    static func testStartFailureReachesBridge() async {
        assert(LocalServiceStarter.lastError(inLogText: "Starting...\nError: chatbot is not installed. Run: uv sync\n")
            == "chatbot is not installed. Run: uv sync")
        assert(LocalServiceStarter.lastError(inLogText: "Error: port 8766 is other.\n") == "port 8766 is other")
        assert(LocalServiceStarter.lastError(inLogText: "Starting...\nVoice backend on port 8766.\n") == nil)

        // A start() that throws (the local service never came up) must emit
        // `error`, or Pi stays on "connecting" forever.
        struct Boom: LocalizedError { var errorDescription: String? { "Local service startup failed." } }
        let backend = MockVoiceBackend()
        backend.startError = Boom()
        let session = SessionController(backend: backend)
        let bridge = HeadlessBridge()
        var emitted = [[String: Any]]()
        bridge.emitSink = { emitted.append($0) }
        bridge.attach(session: session, listenToStdin: false)
        await session.begin()
        let errors = emitted.filter { ($0["type"] as? String) == "error" }
        assert(errors.count == 1, "start failure emits exactly one error event")
        assert((errors.first?["message"] as? String) == "Local service startup failed.")
        assert(!emitted.contains { ($0["type"] as? String) == "ready" })
        assert(session.state == .failed("Local service startup failed."))

        // A backend that already reported its failure is not reported twice.
        let reporting = MockVoiceBackend()
        reporting.startError = Boom()
        let reportingSession = SessionController(backend: reporting)
        let reportingBridge = HeadlessBridge()
        var reported = [[String: Any]]()
        reportingBridge.emitSink = { reported.append($0) }
        reportingBridge.attach(session: reportingSession, listenToStdin: false)
        let original = reporting.onState
        reporting.onState = { state in
            original?(state)
            if state == .connecting { original?(.failed("Local service startup failed.")) }
        }
        await reportingSession.begin()
        assert(reported.filter { ($0["type"] as? String) == "error" }.count == 1, "no duplicate error event")
    }

    @MainActor
    static func testSessionConversationHistory() async {
        let backend = MockVoiceBackend()
        let session = SessionController(backend: backend)
        backend.onState?(.listening)

        // Simulate a turn: user speaks, agent replies, tool runs
        backend.onUserSpeechStarted?()
        backend.onUserFinal?("what is the weather", "item-h1")
        backend.onAgentDelta?("Checking the radar for you.")
        backend.onAgentDone?()
        backend.onToolDone?("bash", "temperature is 72 degrees")

        // Turn settling: second user turn
        backend.onUserFinal?("and tomorrow", "item-h2")
        backend.onAgentDelta?("Tomorrow will be sunny.")
        backend.onAgentDone?()

        // Reconnect session: begin() passes history messages to backend
        await session.begin()
        assert(backend.historyMessages.count >= 4, "Session history should have user, assistant, and tool turns")
        assert(backend.historyMessages.contains { $0.role == "user" && $0.text.contains("what is the weather") })
        assert(backend.historyMessages.contains { $0.role == "assistant" && $0.text.contains("Checking the radar") })
        assert(backend.historyMessages.contains { $0.role == "tool" && $0.name == "bash" && $0.text.contains("72 degrees") })
        assert(backend.historyMessages.contains { $0.role == "user" && $0.text.contains("and tomorrow") })
        assert(backend.historyMessages.contains { $0.role == "assistant" && $0.text.contains("Tomorrow will be sunny") })

        let restartedBackend = MockVoiceBackend()
        let restarted = SessionController(backend: restartedBackend)
        restarted.seedHistory([
            (role: "user", text: "We discussed AI news."),
            (role: "assistant", text: "Yes, we discussed AI news."),
        ])
        await restarted.begin()
        assert(restartedBackend.historyMessages.map(\.text) == [
            "We discussed AI news.", "Yes, we discussed AI news."
        ], "a new Voice process must replay Pi's saved turns")
        let manyBackend = MockVoiceBackend()
        let many = SessionController(backend: manyBackend)
        let turns = (0..<80).map { (role: $0 % 2 == 0 ? "user" : "assistant", text: "turn \($0)") }
        many.seedHistory(turns)
        await many.begin()
        assert(manyBackend.historyMessages.map(\.text) == turns.map(\.text), "Swift preserves upstream replay selection")
    }

    @MainActor
    static func testPiJobTracker() {
        let t0 = Date(timeIntervalSince1970: 1_000_000)
        var tracker = PiJobTracker()

        // Idle with nothing ever asked.
        let idle = tracker.statusPayload(now: t0)
        assert((idle["status"] as? String) == "idle", "no jobs is idle")
        assert(idle["last"] == nil, "no last job without results")

        // Ask moves to queued with elapsed seconds.
        tracker.ask(id: "w1", brief: "map the project", now: t0)
        assert(tracker.hasActive, "asked job is active")
        let queued = tracker.statusPayload(now: t0.addingTimeInterval(42))
        assert((queued["status"] as? String) == "queued")
        assert((queued["id"] as? String) == "w1")
        assert((queued["elapsed_s"] as? Int) == 42)
        assert(((queued["brief"] as? String) ?? "").contains("map the project"))

        // Extension phase updates flow into status, notes included.
        tracker.update(id: "w1", state: .working, note: "using bash", now: t0.addingTimeInterval(50))
        let working = tracker.statusPayload(now: t0.addingTimeInterval(61))
        assert((working["status"] as? String) == "working")
        assert((working["detail"] as? String) == "using bash")
        assert((working["elapsed_s"] as? Int) == 61)

        // The extension owns supersede: a second ask leaves the first alone
        // locally, and the extension's supersede update sticks over stale news.
        tracker.ask(id: "w2", brief: "now Chrome", now: t0.addingTimeInterval(70))
        var current = tracker.statusPayload(now: t0.addingTimeInterval(71))
        assert((current["id"] as? String) == "w2", "latest ask is current")
        tracker.update(id: "w1", state: .superseded, note: nil, now: t0.addingTimeInterval(72))
        tracker.update(id: "w1", state: .working, note: "stale", now: t0.addingTimeInterval(73))
        current = tracker.statusPayload(now: t0.addingTimeInterval(74))
        assert((current["id"] as? String) == "w2", "stale update must not resurrect w1")

        // A dropped ask (extension declined it, e.g. mic closed) clears active.
        tracker.update(id: "w2", state: .dropped, note: nil, now: t0.addingTimeInterval(75))
        assert(!tracker.hasActive, "dropped job clears active")
        tracker.ask(id: "w2b", brief: "retry", now: t0.addingTimeInterval(76))

        // Finish stores the result and clears active; status reports last.
        tracker.finish(id: "w2b", text: "Chrome shows the weather.", now: t0.addingTimeInterval(80))
        assert(!tracker.hasActive, "finished job clears active")
        let done = tracker.statusPayload(now: t0.addingTimeInterval(100))
        assert((done["status"] as? String) == "idle")
        assert(((done["last"] as? [String: Any])?["id"] as? String) == "w2b")
        assert(((done["last"] as? [String: Any])?["outcome"] as? String) == "done")

        // A newer failed job must not be hidden by an older cached result.
        tracker.ask(id: "w2c", brief: "failed follow-up", now: t0.addingTimeInterval(85))
        tracker.update(id: "w2c", state: .failed, note: "Pi finished with no answer", now: t0.addingTimeInterval(86))
        let latest = tracker.statusPayload(now: t0.addingTimeInterval(87))
        assert(((latest["last"] as? [String: Any])?["id"] as? String) == "w2c")
        assert(((latest["last"] as? [String: Any])?["outcome"] as? String) == "failed")
        tracker.finish(id: "w2c", text: "Partial findings", now: t0.addingTimeInterval(88))
        assert(tracker.jobs["w2c"]?.state == .failed, "Caching partial findings preserves terminal outcome")
        assert((tracker.resultsPayload(id: "w2c", cursor: 0, limit: 100)["text"] as? String) == "Partial findings")

        // Paged re-reads, defaulting to latest.
        let long = String(repeating: "word ", count: 2500) + "end."
        tracker.finish(id: "w3", brief: "long one", text: long, now: t0.addingTimeInterval(90))
        let page1 = tracker.resultsPayload(id: nil, cursor: 0, limit: 1500)
        assert((page1["id"] as? String) == "w3", "nil id reads latest")
        assert((page1["done"] as? Bool) == false, "long answer does not fit one page")
        assert(((page1["text"] as? String) ?? "").count == 1500)
        let cursor = (page1["cursor"] as? Int) ?? -1
        assert(cursor == 1500, "cursor advances by page chars")
        var assembled = page1["text"] as? String ?? ""
        var next = cursor
        while next < long.count {
            let page = tracker.resultsPayload(id: "w3", cursor: next, limit: 4000)
            assembled += page["text"] as? String ?? ""
            next = page["cursor"] as? Int ?? next
        }
        assert(assembled == long, "paged result must reconstruct the full answer beyond the old 6000-character limit")
        assert((page1["total_chars"] as? Int) == long.count)
        let unknown = tracker.resultsPayload(id: "nope", cursor: 0, limit: 10)
        assert((unknown["error"] as? String) == "unknown_id")
        assert(!((unknown["ids"] as? [String]) ?? []).isEmpty, "unknown id lists available ids")

        // A job that dies with no answer reports failed, not bare idle.
        var failedTracker = PiJobTracker()
        failedTracker.ask(id: "f1", brief: "doomed errand", now: t0)
        failedTracker.update(id: "f1", state: .working, note: "using bash", now: t0)
        failedTracker.update(
            id: "f1", state: .failed, note: "Pi finished with no answer", now: t0.addingTimeInterval(5)
        )
        assert(!failedTracker.hasActive, "failed job clears active")
        let failedStatus = failedTracker.statusPayload(now: t0.addingTimeInterval(9))
        assert((failedStatus["status"] as? String) == "idle")
        let lastFailed = failedStatus["last"] as? [String: Any]
        assert(lastFailed?["outcome"] as? String == "failed", "Luna must see failure, not idle")
        assert((lastFailed?["detail"] as? String) == "Pi finished with no answer")
        assert(((lastFailed?["brief"] as? String) ?? "").contains("doomed errand"))

        // A second handoff during an active job steers it; the ack says so and
        // never reads like a second job lining up behind the first.
        let fresh = PiJobTracker.handoffAck(id: "a1", steering: false)
        assert((fresh["status"] as? String) == "started")
        assert((fresh["id"] as? String) == "a1")
        let steer = PiJobTracker.handoffAck(id: "a2", steering: true)
        assert((steer["status"] as? String) == "steering")
        assert((steer["id"] as? String) == "a2")
        let steerNote = (steer["note"] as? String) ?? ""
        assert(steerNote.hasPrefix("This was sent to steer the current Pi task."), steerNote)
        assert(steerNote.contains("the earlier task is replaced"))
        assert(steerNote.contains("Okay, I've redirected Pi to that instead."))
        for ack in [fresh, steer] {
            let text = ack.values.compactMap { $0 as? String }.joined(separator: " ").lowercased()
            for banned in ["queue", "after that", "next", "waiting"] {
                assert(!text.contains(banned), "handoff ack must not say \(banned): \(text)")
            }
        }
        var steering = PiJobTracker()
        steering.ask(id: "s1", brief: "search for FIXME", now: t0)
        assert(steering.hasActive, "a running job means the next handoff steers")
        steering.update(id: "s1", state: .superseded, note: "Task updated; Pi continues", now: t0)
        assert(!steering.hasActive)

        // Human elapsed.
        assert(PiJobTracker.elapsedString(since: t0, now: t0.addingTimeInterval(9)) == "9s")
        assert(PiJobTracker.elapsedString(since: t0, now: t0.addingTimeInterval(80)) == "1m20s")

        // Journal appends and trims.
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("voice-tracker-tests-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let line = PiJobTracker.journalLine(now: t0, event: "queued", id: "w1", brief: "map the project")
        assert(line.contains("\"event\":\"queued\""), "journal line is JSON")
        PiJobTracker.appendJournal(directory: dir, line: line)
        PiJobTracker.appendJournal(directory: dir, line: String(repeating: "x", count: 100), maxBytes: 64)
        let logged = (try? String(contentsOf: dir.appendingPathComponent(PiJobTracker.journalFileName), encoding: .utf8)) ?? ""
        assert(logged.count <= 128, "oversize journal trims back, got \(logged.count)")
        try? FileManager.default.removeItem(at: dir)
    }
}
