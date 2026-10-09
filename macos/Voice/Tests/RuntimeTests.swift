import Foundation

@main
struct RuntimeTests {
    @MainActor
    static func main() async throws {
        let standardVoice = URL(string: "ws://127.0.0.1:8766/v1/realtime")!
        check(LocalServiceStarter.manages(voice: standardVoice))
        check(!LocalServiceStarter.manages(voice: URL(string: "wss://example.com/v1/realtime")!))
        check(!LocalServiceStarter.manages(voice: URL(string: "ws://127.0.0.1:8866/v1/realtime")!))

        // A running service is reused only while it runs this checkout's current code.
        let root = "/Users/me/chatbot"
        let voiceHealth: [String: Any] = ["ready": true, "server_tools": true, "fingerprint": "abc", "stale": false, "source": root]
        check(LocalServiceStarter.voiceStatus(code: 200, json: voiceHealth, root: root) == .ready)
        check(LocalServiceStarter.voiceStatus(code: 200, json: voiceHealth.merging(["ready": false]) { $1 }, root: root) == .starting)
        check(LocalServiceStarter.voiceStatus(code: 200, json: voiceHealth.merging(["ready": false, "server_tools": false]) { $1 }, root: root) == .starting)
        check(LocalServiceStarter.voiceStatus(code: 200, json: voiceHealth.merging(["stale": true]) { $1 }, root: root) == .stale)
        check(LocalServiceStarter.voiceStatus(code: 200, json: voiceHealth.merging(["server_tools": false]) { $1 }, root: root) == .ready)
        check(LocalServiceStarter.voiceStatus(code: 200, json: voiceHealth.merging(["source": "/Users/me/chatbot-refactor"]) { $1 }, root: root) == .stale)
        check(LocalServiceStarter.voiceStatus(code: 200, json: voiceHealth.merging(["source": root + "/"]) { $1 }, root: root) == .ready)
        // Pre-fingerprint servers answer without the contract; they cannot be current.
        check(LocalServiceStarter.voiceStatus(code: 200, json: ["status": "ok", "ready": true], root: root) == .stale)
        check(LocalServiceStarter.voiceStatus(code: 404, json: nil, root: root) == .stale)
        let message = try JSONDecoder().decode(
            ChatMessage.self,
            from: Data(#"{"role":"tool","text":"done","name":"bash"}"#.utf8)
        )
        check(message.role == "tool" && message.text == "done" && message.name == "bash")
        let tracker = PlaybackTracker()
        let old = tracker.enqueue()
        tracker.clear()
        let current = tracker.enqueue()
        check(!tracker.complete(old))
        check(tracker.isAudible, "An abandoned buffer must not drain new playback")
        let now = Date()
        check(tracker.complete(current, now: now))
        check(!tracker.isAudible)
        check(tracker.needsEchoGuard(now: now.addingTimeInterval(0.5)))
        check(!tracker.needsEchoGuard(now: now.addingTimeInterval(1)))
        check(tracker.needsFirstReplyGuard(now: now.addingTimeInterval(0.5)))
        check(!tracker.needsFirstReplyGuard(now: now.addingTimeInterval(1)))
        let later = tracker.enqueue()
        check(!tracker.needsFirstReplyGuard(now: now.addingTimeInterval(1)), "later replies keep normal barge-in")
        check(tracker.complete(later))
        tracker.resetForSession()
        let newFirst = tracker.enqueue()
        check(tracker.needsFirstReplyGuard(), "a new call protects its first reply")
        check(tracker.complete(newFirst))

        check(!VoiceToolFollowUp.shouldSend(pendingTools: 3, responseActive: false))
        check(!VoiceToolFollowUp.shouldSend(pendingTools: 0, responseActive: true))
        check(!VoiceToolFollowUp.shouldSend(pendingTools: 1, responseActive: true))
        check(VoiceToolFollowUp.shouldSend(pendingTools: 0, responseActive: false))
        // A handoff the model made without a word must still be acknowledged
        // (live runs: text='' with spawn_thinking every time), once, and never
        // on top of a reply that already spoke or was cut off.
        check(VoiceToolFollowUp.shouldAcknowledgeHandoff(handoffCalled: true, spokenText: " \n", cancelled: false))
        check(!VoiceToolFollowUp.shouldAcknowledgeHandoff(handoffCalled: true, spokenText: "On it.", cancelled: false))
        check(!VoiceToolFollowUp.shouldAcknowledgeHandoff(handoffCalled: true, spokenText: "", cancelled: true))
        check(!VoiceToolFollowUp.shouldAcknowledgeHandoff(handoffCalled: false, spokenText: "", cancelled: false))
        check(!VoiceToolFollowUp.shouldAcknowledgeHandoff(handoffCalled: true, spokenText: "", cancelled: false, muted: true),
               "a muted handoff is dropped by the extension; do not promise it")
        // Pi's [FINAL] waits while the server answers a transcribed turn it has
        // not announced yet, but never forever.
        // turn_ignored arrives as error.type with a null code; it is not a failure.
        check(VoiceServerError.kind(["type": "turn_ignored", "code": NSNull(), "message": "Turn ignored (no_text)"]) == "turn_ignored")
        check(VoiceServerError.kind(["type": "invalid_request_error", "code": "response_cancel_not_active"]) == "response_cancel_not_active")
        check(VoiceServerError.kind(["message": "x"]) == "")
        let heldAt = Date()
        check(!VoiceContextHold.shouldHold(implicitTurnSince: nil, now: heldAt))
        check(VoiceContextHold.shouldHold(implicitTurnSince: heldAt, now: heldAt.addingTimeInterval(3)))
        check(!VoiceContextHold.shouldHold(implicitTurnSince: heldAt, now: heldAt.addingTimeInterval(VoiceContextHold.maxHold)))

        let scope = VoiceWorkScope()
        let oldGeneration = scope.generation
        let task = Task<Void, Never> { try? await Task.sleep(nanoseconds: 10_000_000_000) }
        scope.insert(task, id: "old")
        scope.cancel()
        check(task.isCancelled && oldGeneration != scope.generation)
        let newTask = Task {}
        scope.insert(newTask, id: "old")
        scope.finish("old", generation: oldGeneration)
        check(scope.contains("old"), "A stale completion must not remove new work")
        scope.cancel()

        // No client-side tools remain: the client only publishes the
        // server-run definitions. Luna no longer sees the screen.
        let names = VoiceToolExecutor.shared.activeToolDefinitions().compactMap { $0["name"] as? String }
        check(Set(names).count == names.count, "Tool names must be unique")
        check(names.contains("bash"), "This branch publishes bash for public research")
        check(!names.contains("web_search"))
        let bash = VoiceToolExecutor.shared.activeToolDefinitions().first { $0["name"] as? String == "bash" }
        let bashDesc = bash?["description"] as? String ?? ""
        check(bashDesc.contains("when:1d"), "bash tool must tell the model to date-filter news")
        check(bashDesc.contains("Wikipedia"), "office-holder facts should fetch Wikipedia, not a news feed")
        check(bashDesc.contains("voice model") || bashDesc.contains("Research the voice model"),
               "bash is the voice model's research tool")
        check(!names.contains("code_agent"), "The coding agent was removed; Claude Code covers that job")
        // Removed with the sidecar that served them; Pi owns web reading and memory.
        check(!names.contains("read_page") && !names.contains("web_fetch") && !names.contains("read_article"),
               "Page-reading tools went with the sidecar")
        check(!names.contains("remember") && !names.contains("forget") && !names.contains("search_chat_history"),
               "Memory tools went with the sidecar")
        check(!names.contains("screenshot"), "The screenshot tool was removed")
        check(VoiceToolExecutor.serverSideTools == ["bash"], "bash is the only tool the server runs")
        for name in names where VoiceToolExecutor.serverSideTools.contains(name) {
            check(["bash", "read_page", "remember", "forget", "search_chat_history"].contains(name))
        }
        let unavailable = await VoiceToolExecutor.shared.run(name: "web_search", argsJson: "{\"query\":\"x\"}")
        check(unavailable.output.contains("runs on the server"), "Research tools never execute in the app")

        testMicCapture()
        testSpeechStartPolicy()
        testTranscript()
        testIdleChromeKeepsMute()
        testHeadlessBridge()
        testContract()
        await testSessionConversationHistory()
        await testStartFailureReachesBridge()
        await testAudioIOProbe()
        testPiJobTracker()
        check(!PiJobTracker.shouldSpeakProgress(elapsed: 7, changed: true))
        check(PiJobTracker.shouldSpeakProgress(elapsed: 8, changed: true))
        check(PiJobTracker.shouldSpeakProgress(elapsed: 20, changed: true))
        check(!PiJobTracker.shouldSpeakProgress(elapsed: 19, changed: false))
        check(!PiJobTracker.shouldSpeakProgress(elapsed: 20, changed: false))
        check(!PiJobTracker.shouldSpeakProgress(elapsed: 60, changed: false))
        let status = PiJobTracker.statusChannel(note: "Pi is reading files")
        check(status.hasPrefix("[STATUS] Pi is reading files."))
        check(status.contains("do not speak it"))
        check(status.contains("This is not the user."))
        check(PiJobTracker.statusChannel(note: "  ").contains("Pi is still working"))
        check(PiJobTracker.failureChannel(reason: "no answer").contains("The task failed: no answer"))
        let done = PiJobTracker.finalChannel(excerpt: "It compiled.", outcome: .done)
        check(done.hasPrefix("[FINAL] Pi finished."))
        check(done.contains("It compiled."))
        let partial = PiJobTracker.finalChannel(excerpt: "halfway", outcome: .stopped)
        check(partial.contains("incomplete task (stopped)"))
        check(!partial.contains("pi_results"))
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
        check(sent != nil, "Accepting capture must emit a batched chunk")
        capture.setMuted(true)
        check(capture.ingest(speech, generation: gen) == nil, "Mute must drop the next ingest without waiting")
        let gen2 = UUID()
        capture.arm(generation: gen2)
        capture.setAccepting(true)
        check(capture.ingest(speech, generation: gen2) == nil, "arm must not unmute")
        capture.setMuted(false)
        var unmuted: [UInt8]?
        for _ in 0..<4 {
            if let chunk = capture.ingest(speech, generation: gen2) { unmuted = chunk }
        }
        check(unmuted != nil, "Unmute after arm must emit again")
        capture.disarm()
        check(capture.ingest(speech, generation: gen) == nil, "Disarmed capture must drop audio")
    }

    static func testSpeechStartPolicy() {
        check(
            !VoiceSpeechStartPolicy.shouldShowListening(responseActive: true, playing: false),
            "TTS chunk gaps during a response must not look like Listening"
        )
        check(
            !VoiceSpeechStartPolicy.shouldShowListening(responseActive: false, playing: true)
        )
        check(
            VoiceSpeechStartPolicy.shouldShowListening(responseActive: false, playing: false)
        )
        check(
            VoiceTurnAdmissionPolicy.shouldApply(id: "adm_1", seen: [], interruptsOutput: true)
        )
        check(
            !VoiceTurnAdmissionPolicy.shouldApply(id: "adm_1", seen: ["adm_1"], interruptsOutput: true)
        )
        check(
            !VoiceTurnAdmissionPolicy.shouldApply(id: "adm_1", seen: [], interruptsOutput: false)
        )
    }

    @MainActor
    static func testIdleChromeKeepsMute() {
        let backend = MockVoiceBackend()
        let session = SessionController(backend: backend)
        backend.onState?(.listening)
        session.setMuted(true)
        check(session.isMuted, "Headless mute must set session mute while live")
        session.requestEnd()
        check(session.isMuted, "going idle must not unmute")
        check(session.state == .idle)
    }

    @MainActor
    static func testTranscript() {
        let backend = MockVoiceBackend()
        let session = SessionController(backend: backend)
        backend.onState?(.listening)
        session.setMuted(true)
        check(session.isMuted, "Headless mute must set session mute while live")
        session.setMuted(false)
        check(!session.isMuted)
        backend.onUserSpeechStarted?()
        backend.onUserFinal?("I want to explain", "turn-one")
        // A final is held until the turn settles. Pausing mid-sentence finalizes
        // each revision, and painting every one rewrote the bubble while the
        // user was still talking.
        check(session.turns.isEmpty, "A finalized turn waits for the turn to settle")
        check(session.userSpeaking, "The indicator stays up while the turn is held")
        backend.onAgentDelta?("Go ahead, I am listening carefully to the microphone problem.")
        check(!session.userSpeaking, "Painting the words lowers the indicator")
        let original = session.turns[0].id
        check(session.turns[0].text == "I want to explain")
        backend.onAgentDone?()
        backend.onUserFinal?("I want to explain the microphone problem", "turn-one")
        backend.onTurnDropped?()
        check(session.turns.count == 2 && session.turns[0].id == original)
        backend.onUserFinal?("Now read this other page", "turn-two")
        backend.onTurnDropped?()
        check(session.turns.count == 3, "A distinct utterance must not overwrite earlier speech")

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
        check(repeats.turns.isEmpty, "Revisions must not paint while the turn is still open")
        check(repeats.userSpeaking, "The indicator covers the pauses")
        repeatBackend.onTurnDropped?()
        let shown = repeats.turns[0].text
        check(repeats.turns.count == 1, "Pausing inside one turn must not open more bubbles")
        check(
            shown == "\(head) and see whether it's good it's still are the other things over there",
            "Each revision must replace the bubble; got: \(shown)"
        )
        check(
            shown.components(separatedBy: "i'm sorry").count - 1 == 1,
            "The turn must appear exactly once, not once per pause"
        )

        let pauseBackend = MockVoiceBackend()
        let pauseSession = SessionController(backend: pauseBackend)
        pauseBackend.onUserFinal?("yeah i still need to finish", "turn-a")
        pauseBackend.onUserFinal?("a lot of work to do", "turn-b")
        pauseBackend.onTurnDropped?()
        check(pauseSession.turns.count == 1, "A paused continuation must stay one bubble")
        check(pauseSession.turns[0].text.contains("yeah i still need to finish"))
        check(pauseSession.turns[0].text.contains("a lot of work to do"))

        let fillerBackend = MockVoiceBackend()
        let fillerSession = SessionController(backend: fillerBackend)
        fillerBackend.onUserFinal?("i still need to finish", "turn-fill-a")
        fillerBackend.onAgentDelta?("...")
        fillerBackend.onAgentDone?()
        fillerBackend.onUserFinal?("a lot of work today", "turn-fill-b")
        fillerBackend.onTurnDropped?()
        check(fillerSession.turns.count == 2, "Keep the tiny agent filler row")
        check(fillerSession.turns[0].speaker == .you)
        check(fillerSession.turns[0].text.contains("i still need to finish"))
        check(fillerSession.turns[0].text.contains("a lot of work today"))
        check(fillerSession.turns[1].text == "...")

        let restateBackend = MockVoiceBackend()
        let restateSession = SessionController(backend: restateBackend)
        restateBackend.onUserFinal?("i still need to finish a lot of work to do", "turn-restate")
        restateBackend.onUserFinal?("i still need to finish a lot of work today", "turn-restate")
        restateBackend.onTurnDropped?()
        check(restateSession.turns.count == 1)
        check(restateSession.turns[0].text == "i still need to finish a lot of work today")

        let splitBackend = MockVoiceBackend()
        let splitSession = SessionController(backend: splitBackend)
        splitBackend.onUserFinal?("yeah i still need to finish", "turn-split-a")
        splitBackend.onTurnDropped?()
        splitBackend.onUserFinal?("a lot of work to do", "turn-split-b")
        splitBackend.onTurnDropped?()
        check(splitSession.turns.count == 1, "A paused continuation must stay one bubble")
        check(splitSession.turns[0].text.contains("yeah i still need to finish"))
        check(splitSession.turns[0].text.contains("a lot of work to do"))

        // The talking indicator is the only speaking feedback, so it must never
        // strand: every way a turn can end has to lower it.
        let barsBackend = MockVoiceBackend()
        let bars = SessionController(backend: barsBackend)
        check(!bars.userSpeaking, "Idle shows no indicator")
        barsBackend.onUserSpeechStarted?()
        check(bars.userSpeaking, "Speech start raises the indicator")
        barsBackend.onTurnDropped?()
        check(!bars.userSpeaking, "A turn the server drops lowers the indicator")
        barsBackend.onUserSpeechStarted?()
        barsBackend.onUserFinal?("what is left on my list", "turn-bars")
        check(bars.userSpeaking, "A held final keeps the indicator up through a pause")
        check(bars.turns.isEmpty, "and paints nothing yet")
        barsBackend.onAgentDelta?("Two things.")
        check(!bars.userSpeaking, "Painting the words lowers the indicator")
        check(bars.turns.first?.text == "what is left on my list")
        barsBackend.onUserSpeechStarted?()
        barsBackend.onUserFinal?("one more thing", "turn-bars-2")
        check(bars.userSpeaking)
        bars.requestEnd()
        check(!bars.userSpeaking, "Stopping lowers the indicator")
        check(
            bars.turns.contains { $0.text.contains("one more thing") },
            "Stopping must paint held words rather than losing them"
        )

        let junkBackend = MockVoiceBackend()
        let junkSession = SessionController(backend: junkBackend)
        // The server drops filler before it reaches the client, so a dropped
        // turn must leave no bubble and no indicator behind.
        junkBackend.onUserSpeechStarted?()
        junkBackend.onTurnDropped?()
        check(junkSession.turns.isEmpty, "A dropped turn leaves no bubble")
        check(!junkSession.userSpeaking, "A dropped turn lowers the indicator")
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
        check(emitted.contains { ($0["type"] as? String) == "ready" }, "Bridge emits ready when backend listens")

        // Wire contract (out): heard
        emitted.removeAll()
        backend.onUserFinal?("hello pi", "item-1")
        check(emitted.count == 1, "heard event emitted")
        check((emitted[0]["type"] as? String) == "heard")
        check((emitted[0]["text"] as? String) == "hello pi")
        check((emitted[0]["item_id"] as? String) == "item-1")

        // Wire contract (out): spoken
        emitted.removeAll()
        backend.onAgentDelta?("hello")
        check(emitted.count == 1, "spoken delta emitted before completion")
        check((emitted[0]["type"] as? String) == "spoken_delta")
        check((emitted[0]["text"] as? String) == "hello")
        let spokenID = emitted[0]["item_id"] as? String
        check(!(spokenID ?? "").isEmpty)
        emitted.removeAll()
        backend.onSpoken?("hello human")
        check(emitted.count == 1, "spoken event emitted")
        check((emitted[0]["type"] as? String) == "spoken")
        check((emitted[0]["text"] as? String) == "hello human")
        check((emitted[0]["item_id"] as? String) == spokenID)

        // Wire contract (out): work
        emitted.removeAll()
        backend.onSpawnThinking?("call-1", "check system status")
        check(emitted.count == 1, "work event emitted")
        check((emitted[0]["type"] as? String) == "work")
        check((emitted[0]["id"] as? String) == "call-1")
        check((emitted[0]["brief"] as? String) == "check system status")

        // Wire contract (in): mute
        check(!session.isMuted)
        bridge.handle(line: #"{"type":"mute","muted":true}"#)
        check(session.isMuted, "Mute line mutes session")
        bridge.handle(line: #"{"type":"mute","muted":false}"#)
        check(!session.isMuted, "Unmute line unmutes session")

        // Wire contract (in): interrupt
        let priorInterrupts = backend.interruptCount
        bridge.handle(line: #"{"type":"interrupt"}"#)
        check(backend.interruptCount > priorInterrupts, "Interrupt line interrupts backend")

        // Wire contract (in): user
        let priorUserInterrupts = backend.interruptCount
        bridge.handle(line: #"{"type":"user","text":"write a poem"}"#)
        check(backend.interruptCount > priorUserInterrupts, "User line interrupts session")
        check(backend.ingestedUserText.contains("write a poem"), "User line ingests text")

        // Wire contract (in): result
        bridge.handle(line: #"{"type":"result","id":"call-1","speak":"here is the poem","full":"here is the complete poem"}"#)
        check(backend.postedResults.contains { $0.id == "call-1" && $0.speak == "here is the poem" && $0.full == "here is the complete poem" }, "Result line keeps both the spoken preview and complete result")
        let fullReport = String(repeating: "long detail ", count: 1000)
        let reportLine = try! JSONSerialization.data(withJSONObject: [
            "type": "result", "id": "call-2", "speak": "summary", "full": fullReport
        ])
        bridge.handle(line: reportLine)
        check(backend.postedResults.last?.full == fullReport, "bridge preserves a result longer than 6000 characters")

        // Wire contract (in): job_update
        bridge.handle(line: #"{"type":"job_update","id":"call-1","status":"working","note":"using bash"}"#)
        check(backend.piJobUpdates.count == 1, "job_update line forwards phase")
        check(backend.piJobUpdates[0].id == "call-1")
        check(backend.piJobUpdates[0].status == "working")
        check(backend.piJobUpdates[0].note == "using bash")
        // Missing id or status is ignored, like any malformed line.
        bridge.handle(line: #"{"type":"job_update","status":"working"}"#)
        bridge.handle(line: #"{"type":"job_update","id":"call-1"}"#)
        check(backend.piJobUpdates.count == 1, "job_update without id/status is ignored")

        // Malformed lines and unknown types ignored safely
        bridge.handle(line: "not json at all")
        bridge.handle(line: "{broken json")
        bridge.handle(line: "")
        bridge.handle(line: #"{"type":"future_unknown_command","data":123}"#)
        bridge.handle(line: #"{"no_type_field":true}"#)

        // Wire contract (out): speech_started
        emitted.removeAll()
        backend.onUserSpeechStarted?()
        check(emitted.count == 1, "speech_started event emitted")
        check((emitted[0]["type"] as? String) == "speech_started")

        // Wire contract (out): stop_work
        emitted.removeAll()
        backend.onStopThinking?()
        check(emitted.count == 1, "stop_work event emitted")
        check((emitted[0]["type"] as? String) == "stop_work")

        // Wire contract (out): error
        emitted.removeAll()
        backend.onState?(.failed("socket connection lost"))
        check(emitted.count == 1, "error event emitted")
        check((emitted[0]["type"] as? String) == "error")
        check((emitted[0]["message"] as? String) == "socket connection lost")

        // Reattaching bridge does not duplicate event emissions
        bridge.attach(session: session, listenToStdin: false)
        emitted.removeAll()
        backend.onUserFinal?("hello again", "item-reattach")
        check(emitted.count == 1, "reattached bridge must emit exactly one heard event")
        check((emitted[0]["type"] as? String) == "heard")
    }

    @MainActor
    static func testContract() {
        let thisFile = URL(fileURLWithPath: #filePath)
        let candidates = [
            thisFile.deletingLastPathComponent().appendingPathComponent("../../../contracts/pi-voice.json").standardized,
            thisFile.deletingLastPathComponent().appendingPathComponent("../../contracts/pi-voice.json").standardized,
        ]
        let contractURL = candidates.first(where: { FileManager.default.fileExists(atPath: $0.path) }) ?? candidates[0]
        guard let data = try? Data(contentsOf: contractURL),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let rawEnums = json["enums"] as? [String: Any],
              let stdio = json["stdio"] as? [String: Any],
              let rawToVoice = stdio["toVoice"] as? [String: Any],
              let rawFromVoice = stdio["fromVoice"] as? [String: Any],
              let voiceHistory = json["voiceHistory"] as? [String: Any],
              let rawTools = json["tools"] as? [String: Any],
              let rawChannels = json["channels"] as? [String: Any]
        else {
            check(false, "Failed to load contracts/pi-voice.json at \(contractURL.path)")
            return
        }

        var enums: [String: [String]] = [:]
        for (k, v) in rawEnums where !k.hasPrefix("_") {
            if let arr = v as? [String] { enums[k] = arr }
        }

        var toVoiceSchema: [String: [String: String]] = [:]
        for (k, v) in rawToVoice where !k.hasPrefix("_") {
            if let dict = v as? [String: String] { toVoiceSchema[k] = dict }
        }

        var fromVoiceSchema: [String: [String: String]] = [:]
        for (k, v) in rawFromVoice where !k.hasPrefix("_") {
            if let dict = v as? [String: String] { fromVoiceSchema[k] = dict }
        }

        var tools: [String: [String: String]] = [:]
        for (k, v) in rawTools where !k.hasPrefix("_") {
            if let dict = v as? [String: String] { tools[k] = dict }
        }

        var channels: [String: String] = [:]
        for (k, v) in rawChannels where !k.hasPrefix("_") {
            if let str = v as? String { channels[k] = str }
        }

        func sampleValues(fieldName: String, typeDef: String) -> [Any] {
            let isOptional = typeDef.hasSuffix("?")
            let baseType = isOptional ? String(typeDef.dropLast()) : typeDef
            if baseType == "string" {
                return ["sample-\(fieldName)"]
            } else if baseType == "boolean" {
                return [true, false]
            } else if let enumVals = enums[baseType] {
                return enumVals
            }
            check(false, "Unknown typeDef '\(typeDef)' for field '\(fieldName)'")
            return []
        }

        func generateSamples(schema: [String: String]) -> [[String: Any]] {
            if schema.isEmpty { return [[:]] }
            var results: [[String: Any]] = [[:]]
            for (key, typeDef) in schema {
                let isOptional = typeDef.hasSuffix("?")
                let vals = sampleValues(fieldName: key, typeDef: typeDef)
                var next: [[String: Any]] = []
                for obj in results {
                    if isOptional {
                        next.append(obj)
                    }
                    for val in vals {
                        var copy = obj
                        copy[key] = val
                        next.append(copy)
                    }
                }
                results = next
            }
            return results
        }

        // 1. Every toVoice sample through HeadlessBridge.handle(line:) with MockVoiceBackend + SessionController
        let backend = MockVoiceBackend()
        let session = SessionController(backend: backend)
        backend.onState?(.listening)

        let bridge = HeadlessBridge()
        bridge.emitSink = { _ in }
        var stopServiceCount = 0
        var terminateCount = 0
        bridge.onStopService = { stopServiceCount += 1 }
        bridge.onTerminate = { terminateCount += 1 }
        bridge.attach(session: session, listenToStdin: false)

        let orderedMessageTypes = toVoiceSchema.keys.filter { $0 != "quit" } + (toVoiceSchema.keys.contains("quit") ? ["quit"] : [])
        for msgType in orderedMessageTypes {
            guard let schema = toVoiceSchema[msgType] else { continue }
            let samples = generateSamples(schema: schema)
            check(!samples.isEmpty, "No samples generated for toVoice \(msgType)")
            for sample in samples {
                backend.onState?(.listening)
                var payload: [String: Any] = sample
                payload["type"] = msgType
                let lineData = try! JSONSerialization.data(withJSONObject: payload)
                let priorInterrupts = backend.interruptCount
                bridge.handle(line: lineData)

                switch msgType {
                case "user":
                    let text = sample["text"] as? String ?? ""
                    check(backend.ingestedUserText.contains(text), "backend must ingest user text: \(text)")
                case "mute":
                    let muted = sample["muted"] as? Bool ?? false
                    check(backend.mutedCalls.contains(muted), "backend must record setMuted(\(muted))")
                case "interrupt":
                    check(backend.interruptCount > priorInterrupts, "backend must record interrupt")
                case "result":
                    let id = sample["id"] as? String ?? ""
                    let speak = sample["speak"] as? String ?? ""
                    let full = sample["full"] as? String ?? ""
                    check(backend.postedResults.contains { $0.id == id && $0.speak == speak && $0.full == full },
                          "backend must record result with id=\(id) speak=\(speak) full=\(full)")
                case "job_update":
                    let id = sample["id"] as? String ?? ""
                    let status = sample["status"] as? String ?? ""
                    let note = sample["note"] as? String
                    check(backend.piJobUpdates.contains { $0.id == id && $0.status == status && $0.note == note },
                          "backend must record job_update with id=\(id) status=\(status) note=\(String(describing: note))")
                case "quit":
                    check(terminateCount > 0, "quit must call onTerminate")
                    check(stopServiceCount > 0, "quit must call onStopService")
                default:
                    check(false, "Unrecognized toVoice message type: \(msgType)")
                }
            }
        }

        // Explicit check for jobStatus enum coverage in piJobUpdates
        if let jobStatuses = enums["jobStatus"] {
            for status in jobStatuses {
                check(backend.piJobUpdates.contains { $0.status == status },
                      "backend.piJobUpdates missing update for jobStatus '\(status)'")
            }
        }
        check(backend.mutedCalls.contains(true) && backend.mutedCalls.contains(false),
              "backend.mutedCalls must contain both true and false")

        // 2. Every bridge emission seen while firing each backend callback
        let emitBackend = MockVoiceBackend()
        let emitSession = SessionController(backend: emitBackend)
        let emitBridge = HeadlessBridge()
        var emitted: [[String: Any]] = []
        emitBridge.emitSink = { emitted.append($0) }
        emitBridge.attach(session: emitSession, listenToStdin: false)

        emitBackend.onState?(.listening)
        emitBackend.onState?(.failed("contract-test-error"))
        emitBackend.onRequestError?("contract-req-error")
        emitBackend.onUserSpeechStarted?()
        emitBackend.onUserFinal?("sample-heard-text", "sample-item-1")
        emitBackend.onAgentDelta?("sample-delta-text")
        emitBackend.onSpoken?("sample-spoken-text")
        emitBackend.onAgentDone?()
        emitBackend.onSpawnThinking?("sample-work-id", "sample-work-brief")
        emitBackend.onStopThinking?()

        var emittedTypes = Set<String>()
        for obj in emitted {
            guard let type = obj["type"] as? String else {
                check(false, "Bridge emitted object without 'type': \(obj)")
                continue
            }
            emittedTypes.insert(type)
            guard let schema = fromVoiceSchema[type] else {
                check(false, "Bridge emitted type '\(type)' not in contract stdio.fromVoice")
                continue
            }
            var expectedKeys = Set(schema.keys)
            expectedKeys.insert("type")
            let actualKeys = Set(obj.keys)
            check(actualKeys == expectedKeys,
                  "Emitted keys for '\(type)' do not match contract. Expected \(expectedKeys), got \(actualKeys)")
        }

        for contractType in fromVoiceSchema.keys {
            check(emittedTypes.contains(contractType),
                  "Contract fromVoice type '\(contractType)' was never emitted by bridge")
        }

        // 3. PiJobTracker.State raw values equal enums.jobStatus
        if let jobStatuses = enums["jobStatus"] {
            for status in jobStatuses {
                check(PiJobTracker.State(rawValue: status) != nil,
                      "PiJobTracker.State missing case for contract status '\(status)'")
            }
            let allCases: [PiJobTracker.State] = [
                .queued, .working, .done, .stopped, .superseded, .dropped, .failed
            ]
            check(allCases.count == jobStatuses.count,
                  "PiJobTracker.State known case count (\(allCases.count)) != contract jobStatus count (\(jobStatuses.count))")
            for c in allCases {
                check(jobStatuses.contains(c.rawValue),
                      "PiJobTracker.State case '\(c.rawValue)' not in contract enums.jobStatus")
            }
        } else {
            check(false, "Contract missing enums.jobStatus")
        }

        // 4. VoiceHistoryEnv.variable equals voiceHistory.env and parse keeps sample built from voiceHistory.turn
        if let envVar = voiceHistory["env"] as? String,
           let turnSchema = voiceHistory["turn"] as? [String: String]
        {
            check(VoiceHistoryEnv.variable == envVar,
                  "VoiceHistoryEnv.variable '\(VoiceHistoryEnv.variable)' != contract '\(envVar)'")
            let turnSamples = generateSamples(schema: turnSchema)
            check(!turnSamples.isEmpty, "No turn samples generated for voiceHistory.turn")
            for turnSample in turnSamples {
                let jsonArrayData = try! JSONSerialization.data(withJSONObject: [turnSample])
                let jsonString = String(data: jsonArrayData, encoding: .utf8)
                let parsedTurns = VoiceHistoryEnv.parse(jsonString)
                check(parsedTurns.count == 1, "VoiceHistoryEnv.parse must keep sample turn")
                check(parsedTurns[0].role == turnSample["role"] as? String, "Turn role mismatch")
                check(parsedTurns[0].text == turnSample["text"] as? String, "Turn text mismatch")
            }
        } else {
            check(false, "Contract missing voiceHistory section")
        }

        // 5. HeadlessTools.definitions names equal tools keys with exactly contract argument keys, and spawnBrief reads contract argument
        let defs = HeadlessTools.definitions
        let defNames = Set(defs.compactMap { $0["name"] as? String })
        let toolNames = Set(tools.keys)
        check(defNames == toolNames, "HeadlessTools.definitions names (\(defNames)) != contract tools (\(toolNames))")

        for def in defs {
            guard let name = def["name"] as? String,
                  let contractArgs = tools[name]
            else { continue }
            let params = def["parameters"] as? [String: Any]
            let props = params?["properties"] as? [String: Any] ?? [:]
            let actualArgKeys = Set(props.keys)
            let expectedArgKeys = Set(contractArgs.keys)
            check(actualArgKeys == expectedArgKeys,
                  "Tool '\(name)' argument keys mismatch: expected \(expectedArgKeys), got \(actualArgKeys)")
        }

        let briefArg = tools["spawn_thinking"]?.keys.first ?? "brief"
        let sampleBrief = "contract-brief-test-val"
        let briefJsonData = try! JSONSerialization.data(withJSONObject: [briefArg: sampleBrief])
        let briefJson = String(data: briefJsonData, encoding: .utf8)!
        check(HeadlessTools.spawnBrief(briefJson) == sampleBrief,
              "HeadlessTools.spawnBrief must read contract brief argument '\(briefArg)'")

        // 6. PiJobTracker.statusChannel(note:) and finalChannel(excerpt:outcome:) start with channels.status and channels.final
        if let statusTag = channels["status"],
           let finalTag = channels["final"]
        {
            let statusResult = PiJobTracker.statusChannel(note: "testing progress")
            check(statusResult.hasPrefix(statusTag),
                  "statusChannel must start with '\(statusTag)': \(statusResult)")

            let finalDone = PiJobTracker.finalChannel(excerpt: "finished task", outcome: .done)
            check(finalDone.hasPrefix(finalTag),
                  "finalChannel(.done) must start with '\(finalTag)': \(finalDone)")

            let finalStopped = PiJobTracker.finalChannel(excerpt: "stopped task", outcome: .stopped)
            check(finalStopped.hasPrefix(finalTag),
                  "finalChannel(.stopped) must start with '\(finalTag)': \(finalStopped)")
        } else {
            check(false, "Contract missing channels.status or channels.final")
        }
    }

    @MainActor
    static func testStartFailureReachesBridge() async {
        check(LocalServiceStarter.lastError(inLogText: "Starting...\nError: chatbot is not installed. Run: uv sync\n")
            == "chatbot is not installed. Run: uv sync")
        check(LocalServiceStarter.lastError(inLogText: "Error: port 8766 is other.\n") == "port 8766 is other")
        check(LocalServiceStarter.lastError(inLogText: "Starting...\nVoice backend on port 8766.\n") == nil)

        // A start() that throws (the local service never came up) must emit
        // `error`, or Pi stays on "connecting" forever.
        struct Boom: LocalizedError { var errorDescription: String? { "Local service startup failed." } }
        let backend = MockVoiceBackend()
        backend.startError = Boom()
        let session = SessionController(backend: backend)
        let bridge = HeadlessBridge()
        var emitted = [[String: Any]]()
        bridge.emitSink = { emitted.append($0) }
        var nonFatalExitCalled = false
        bridge.onExit = { _ in nonFatalExitCalled = true }
        bridge.attach(session: session, listenToStdin: false)
        await session.begin()
        let errors = emitted.filter { ($0["type"] as? String) == "error" }
        check(errors.count == 1, "start failure emits exactly one error event")
        check((errors.first?["message"] as? String) == "Local service startup failed.")
        check(!emitted.contains { ($0["type"] as? String) == "ready" })
        check(session.state == .failed("Local service startup failed."))
        check(!nonFatalExitCalled, "non-fatal start error (Boom) must NOT call onExit")

        // Fatal start error (NoIO) must emit error before calling onExit(3)
        struct NoIO: FatalStartError, LocalizedError {
            var errorDescription: String? { AudioIOProbe.noIOMessage }
        }
        let fatalBackend = MockVoiceBackend()
        fatalBackend.startError = NoIO()
        let fatalSession = SessionController(backend: fatalBackend)
        let fatalBridge = HeadlessBridge()
        var fatalEmitted = [[String: Any]]()
        fatalBridge.emitSink = { fatalEmitted.append($0) }
        var exitCode: Int32?
        var exitCallCount = 0
        var errorsBeforeExit = 0
        fatalBridge.onExit = { code in
            exitCallCount += 1
            exitCode = code
            errorsBeforeExit = fatalEmitted.filter { ($0["type"] as? String) == "error" }.count
        }
        fatalBridge.attach(session: fatalSession, listenToStdin: false)
        await fatalSession.begin()
        let fatalErrors = fatalEmitted.filter { ($0["type"] as? String) == "error" }
        check(fatalErrors.count == 1, "fatal start failure emits exactly one error event")
        check((fatalErrors.first?["message"] as? String) == AudioIOProbe.noIOMessage)
        check(errorsBeforeExit == 1, "error event was emitted BEFORE onExit was called")
        check(exitCallCount == 1, "onExit was called once")
        check(exitCode == 3, "onExit was called with 3")

        // A backend that already reported its failure is not reported twice.
        let reporting = MockVoiceBackend()
        reporting.startError = Boom()
        let reportingSession = SessionController(backend: reporting)
        let reportingBridge = HeadlessBridge()
        var reported = [[String: Any]]()
        reportingBridge.emitSink = { reported.append($0) }
        var reportingExitCalled = false
        reportingBridge.onExit = { _ in reportingExitCalled = true }
        reportingBridge.attach(session: reportingSession, listenToStdin: false)
        let original = reporting.onState
        reporting.onState = { state in
            original?(state)
            if state == .connecting { original?(.failed("Local service startup failed.")) }
        }
        await reportingSession.begin()
        check(reported.filter { ($0["type"] as? String) == "error" }.count == 1, "no duplicate error event")
        check(!reportingExitCalled)
    }

    static func testAudioIOProbe() async {
        // Returns true immediately with zero sleeps when rendered() is already true
        var sleepCalls1 = 0
        let res1 = await AudioIOProbe.waitForFirstCycle(
            timeout: 2.0,
            pollInterval: 0.02,
            rendered: { true },
            sleep: { _ in sleepCalls1 += 1 }
        )
        check(res1, "AudioIOProbe returns true immediately when rendered() is true")
        check(sleepCalls1 == 0, "AudioIOProbe zero sleeps when rendered() is true")

        // Returns true after k polls
        let k = 5
        var fakeClock2: TimeInterval = 0
        var sleepCalls2 = 0
        let res2 = await AudioIOProbe.waitForFirstCycle(
            timeout: 2.0,
            pollInterval: 0.02,
            rendered: { sleepCalls2 >= k },
            sleep: { interval in
                sleepCalls2 += 1
                fakeClock2 += interval
            }
        )
        check(res2, "AudioIOProbe returns true after k polls")
        check(sleepCalls2 == k, "AudioIOProbe exactly k sleeps; got \(sleepCalls2) vs \(k)")

        // Returns false after the timeout, with an injected fake sleep that advances a fake clock (no real sleeping).
        // Check the number of sleeps is bounded by timeout/pollInterval (+1).
        let timeout: TimeInterval = 2.0
        let pollInterval: TimeInterval = 0.02
        var fakeClock3: TimeInterval = 0
        var sleepCalls3 = 0
        let res3 = await AudioIOProbe.waitForFirstCycle(
            timeout: timeout,
            pollInterval: pollInterval,
            rendered: { false },
            sleep: { interval in
                sleepCalls3 += 1
                fakeClock3 += interval
            }
        )
        check(!res3, "AudioIOProbe returns false after timeout")
        let maxSleeps = Int(ceil(timeout / pollInterval)) + 1
        check(sleepCalls3 <= maxSleeps, "number of sleeps bounded by timeout/pollInterval (+1); got \(sleepCalls3) max \(maxSleeps)")
        check(sleepCalls3 >= Int(ceil(timeout / pollInterval)), "AudioIOProbe ran until timeout")
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
        check(backend.historyMessages.count >= 4, "Session history should have user, assistant, and tool turns")
        check(backend.historyMessages.contains { $0.role == "user" && $0.text.contains("what is the weather") })
        check(backend.historyMessages.contains { $0.role == "assistant" && $0.text.contains("Checking the radar") })
        check(backend.historyMessages.contains { $0.role == "tool" && $0.name == "bash" && $0.text.contains("72 degrees") })
        check(backend.historyMessages.contains { $0.role == "user" && $0.text.contains("and tomorrow") })
        check(backend.historyMessages.contains { $0.role == "assistant" && $0.text.contains("Tomorrow will be sunny") })

        let restartedBackend = MockVoiceBackend()
        let restarted = SessionController(backend: restartedBackend)
        restarted.seedHistory([
            (role: "user", text: "We discussed AI news."),
            (role: "assistant", text: "Yes, we discussed AI news."),
        ])
        await restarted.begin()
        check(restartedBackend.historyMessages.map(\.text) == [
            "We discussed AI news.", "Yes, we discussed AI news."
        ], "a new Voice process must replay Pi's saved turns")
        let manyBackend = MockVoiceBackend()
        let many = SessionController(backend: manyBackend)
        let turns = (0..<80).map { (role: $0 % 2 == 0 ? "user" : "assistant", text: "turn \($0)") }
        many.seedHistory(turns)
        await many.begin()
        check(manyBackend.historyMessages.map(\.text) == turns.map(\.text), "Swift preserves upstream replay selection")
    }

    @MainActor
    static func testPiJobTracker() {
        let t0 = Date(timeIntervalSince1970: 1_000_000)
        var tracker = PiJobTracker()

        // Idle with nothing ever asked.
        let idle = tracker.statusPayload(now: t0)
        check((idle["status"] as? String) == "idle", "no jobs is idle")
        check(idle["last"] == nil, "no last job without results")

        // Ask moves to queued with elapsed seconds.
        tracker.ask(id: "w1", brief: "map the project", now: t0)
        check(tracker.hasActive, "asked job is active")
        let queued = tracker.statusPayload(now: t0.addingTimeInterval(42))
        check((queued["status"] as? String) == "queued")
        check((queued["id"] as? String) == "w1")
        check((queued["elapsed_s"] as? Int) == 42)
        check(((queued["brief"] as? String) ?? "").contains("map the project"))

        // Extension phase updates flow into status, notes included.
        tracker.update(id: "w1", state: .working, note: "using bash", now: t0.addingTimeInterval(50))
        let working = tracker.statusPayload(now: t0.addingTimeInterval(61))
        check((working["status"] as? String) == "working")
        check((working["detail"] as? String) == "using bash")
        check((working["elapsed_s"] as? Int) == 61)

        // The extension owns supersede: a second ask leaves the first alone
        // locally, and the extension's supersede update sticks over stale news.
        tracker.ask(id: "w2", brief: "now Chrome", now: t0.addingTimeInterval(70))
        var current = tracker.statusPayload(now: t0.addingTimeInterval(71))
        check((current["id"] as? String) == "w2", "latest ask is current")
        tracker.update(id: "w1", state: .superseded, note: nil, now: t0.addingTimeInterval(72))
        tracker.update(id: "w1", state: .working, note: "stale", now: t0.addingTimeInterval(73))
        current = tracker.statusPayload(now: t0.addingTimeInterval(74))
        check((current["id"] as? String) == "w2", "stale update must not resurrect w1")

        // A superseded job whose answer Pi had already finished is reported
        // done by the extension: that one move out of superseded is allowed,
        // so the [FINAL] frame says Pi finished, not partial. Nothing else
        // leaves a terminal state.
        var redirected = PiJobTracker()
        redirected.ask(id: "f1", brief: "find FIXME", now: t0)
        redirected.ask(id: "f2", brief: "now TODO", now: t0.addingTimeInterval(1))
        redirected.update(id: "f1", state: .superseded, note: nil, now: t0.addingTimeInterval(2))
        redirected.update(id: "f1", state: .done, note: nil, now: t0.addingTimeInterval(3))
        check(redirected.jobs["f1"]?.state == .done, "finished-before-redirect job becomes done")
        redirected.finish(id: "f1", text: "Two FIXME lines.", now: t0.addingTimeInterval(4))
        check(PiJobTracker.finalChannel(excerpt: "Two FIXME lines.", outcome: redirected.jobs["f1"]?.state).hasPrefix("[FINAL] Pi finished."))
        check(redirected.statusPayload(now: t0.addingTimeInterval(5))["id"] as? String == "f2", "done update must not steal active from the new job")
        redirected.update(id: "f1", state: .superseded, note: nil, now: t0.addingTimeInterval(6))
        check(redirected.jobs["f1"]?.state == .done, "done stays done")
        for sticky in [PiJobTracker.State.stopped, .failed, .dropped] {
            var t = PiJobTracker()
            t.ask(id: "x", brief: "b", now: t0)
            t.update(id: "x", state: sticky, note: nil, now: t0)
            t.update(id: "x", state: .done, note: nil, now: t0)
            check(t.jobs["x"]?.state == sticky, "\(sticky) must not become done")
        }

        // A dropped ask (extension declined it, e.g. mic closed) clears active.
        tracker.update(id: "w2", state: .dropped, note: nil, now: t0.addingTimeInterval(75))
        check(!tracker.hasActive, "dropped job clears active")
        tracker.ask(id: "w2b", brief: "retry", now: t0.addingTimeInterval(76))

        // Finish stores the result and clears active; status reports last.
        tracker.finish(id: "w2b", text: "Chrome shows the weather.", now: t0.addingTimeInterval(80))
        check(!tracker.hasActive, "finished job clears active")
        let done = tracker.statusPayload(now: t0.addingTimeInterval(100))
        check((done["status"] as? String) == "idle")
        check(((done["last"] as? [String: Any])?["id"] as? String) == "w2b")
        check(((done["last"] as? [String: Any])?["outcome"] as? String) == "done")

        // A newer failed job must not be hidden by an older cached result.
        tracker.ask(id: "w2c", brief: "failed follow-up", now: t0.addingTimeInterval(85))
        tracker.update(id: "w2c", state: .failed, note: "Pi finished with no answer", now: t0.addingTimeInterval(86))
        let latest = tracker.statusPayload(now: t0.addingTimeInterval(87))
        check(((latest["last"] as? [String: Any])?["id"] as? String) == "w2c")
        check(((latest["last"] as? [String: Any])?["outcome"] as? String) == "failed")
        tracker.finish(id: "w2c", text: "Partial findings", now: t0.addingTimeInterval(88))
        check(tracker.jobs["w2c"]?.state == .failed, "Caching partial findings preserves terminal outcome")
        check((tracker.resultsPayload(id: "w2c", cursor: 0, limit: 100)["text"] as? String) == "Partial findings")

        // Paged re-reads, defaulting to latest.
        let long = String(repeating: "word ", count: 2500) + "end."
        tracker.finish(id: "w3", brief: "long one", text: long, now: t0.addingTimeInterval(90))
        let page1 = tracker.resultsPayload(id: nil, cursor: 0, limit: 1500)
        check((page1["id"] as? String) == "w3", "nil id reads latest")
        check((page1["done"] as? Bool) == false, "long answer does not fit one page")
        check(((page1["text"] as? String) ?? "").count == 1500)
        let cursor = (page1["cursor"] as? Int) ?? -1
        check(cursor == 1500, "cursor advances by page chars")
        var assembled = page1["text"] as? String ?? ""
        var next = cursor
        while next < long.count {
            let page = tracker.resultsPayload(id: "w3", cursor: next, limit: 4000)
            assembled += page["text"] as? String ?? ""
            next = page["cursor"] as? Int ?? next
        }
        check(assembled == long, "paged result must reconstruct the full answer beyond the old 6000-character limit")
        check((page1["total_chars"] as? Int) == long.count)
        let unknown = tracker.resultsPayload(id: "nope", cursor: 0, limit: 10)
        check((unknown["error"] as? String) == "unknown_id")
        check(!((unknown["ids"] as? [String]) ?? []).isEmpty, "unknown id lists available ids")

        // A job that dies with no answer reports failed, not bare idle.
        var failedTracker = PiJobTracker()
        failedTracker.ask(id: "f1", brief: "doomed errand", now: t0)
        failedTracker.update(id: "f1", state: .working, note: "using bash", now: t0)
        failedTracker.update(
            id: "f1", state: .failed, note: "Pi finished with no answer", now: t0.addingTimeInterval(5)
        )
        check(!failedTracker.hasActive, "failed job clears active")
        let failedStatus = failedTracker.statusPayload(now: t0.addingTimeInterval(9))
        check((failedStatus["status"] as? String) == "idle")
        let lastFailed = failedStatus["last"] as? [String: Any]
        check(lastFailed?["outcome"] as? String == "failed", "Luna must see failure, not idle")
        check((lastFailed?["detail"] as? String) == "Pi finished with no answer")
        check(((lastFailed?["brief"] as? String) ?? "").contains("doomed errand"))

        // A second handoff during an active job steers it; the ack says so and
        // never reads like a second job lining up behind the first.
        let fresh = PiJobTracker.handoffAck(id: "a1", steering: false)
        check((fresh["status"] as? String) == "started")
        check((fresh["id"] as? String) == "a1")
        let steer = PiJobTracker.handoffAck(id: "a2", steering: true)
        check((steer["status"] as? String) == "steering")
        check((steer["id"] as? String) == "a2")
        let steerNote = (steer["note"] as? String) ?? ""
        check(steerNote.hasPrefix("This was sent to steer the current Pi task."), steerNote)
        check(steerNote.contains("the earlier task is replaced"))
        check(steerNote.contains("Okay, I've redirected Pi to that instead."))
        for ack in [fresh, steer] {
            let text = ack.values.compactMap { $0 as? String }.joined(separator: " ").lowercased()
            for banned in ["queue", "after that", "next", "waiting"] {
                check(!text.contains(banned), "handoff ack must not say \(banned): \(text)")
            }
        }
        // A near-identical brief right after the job starts is a repeat: it is
        // answered without steering, so Pi's in-flight search is not thrown away.
        var repeats = PiJobTracker()
        repeats.ask(id: "r1", brief: "Search the web for the latest AI news from today.", now: t0)
        check(repeats.activeRepeat(of: "search the web for the latest AI news from today", now: t0.addingTimeInterval(8)) == "r1")
        check(repeats.activeRepeat(of: "Please search the web for latest AI news today", now: t0.addingTimeInterval(8)) == "r1")
        check(repeats.activeRepeat(of: "Search the web for the latest area news from today.", now: t0.addingTimeInterval(8)) == nil,
               "a word that changes the task still steers")
        check(repeats.activeRepeat(of: "Check the weather in Calgary", now: t0.addingTimeInterval(8)) == nil)
        check(repeats.activeRepeat(of: "Search the web for the latest AI news from today with sources", now: t0.addingTimeInterval(8)) == nil,
               "an added requirement still steers")
        check(repeats.activeRepeat(of: "Search the web for the latest AI news from today.", now: t0.addingTimeInterval(31)) == nil,
               "after the window a repeat is a deliberate redo")
        check(repeats.activeRepeat(of: "", now: t0.addingTimeInterval(1)) == nil)
        repeats.update(id: "r1", state: .done, note: nil, now: t0.addingTimeInterval(2))
        check(repeats.activeRepeat(of: "Search the web for the latest AI news from today.", now: t0.addingTimeInterval(3)) == nil,
               "a finished job is not repeated, it is asked again")
        let repeatAck = PiJobTracker.repeatAck(id: "r1")
        check((repeatAck["status"] as? String) == "already_working")
        check((repeatAck["id"] as? String) == "r1")
        let repeatText = repeatAck.values.compactMap { $0 as? String }.joined(separator: " ").lowercased()
        check(repeatText.contains("nothing was sent"))
        for banned in ["queue", "after that", "next", "waiting", "redirected"] {
            check(!repeatText.contains(banned), "repeat ack must not say \(banned): \(repeatText)")
        }

        var steering = PiJobTracker()
        steering.ask(id: "s1", brief: "search for FIXME", now: t0)
        check(steering.hasActive, "a running job means the next handoff steers")
        steering.update(id: "s1", state: .superseded, note: "Task updated; Pi continues", now: t0)
        check(!steering.hasActive)

        // Human elapsed.
        check(PiJobTracker.elapsedString(since: t0, now: t0.addingTimeInterval(9)) == "9s")
        check(PiJobTracker.elapsedString(since: t0, now: t0.addingTimeInterval(80)) == "1m20s")

        // Journal appends and trims.
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("voice-tracker-tests-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let line = PiJobTracker.journalLine(now: t0, event: "queued", id: "w1", brief: "map the project")
        check(line.contains("\"event\":\"queued\""), "journal line is JSON")
        check(PiJobTracker.journalDirectory(environment: ["TMPDIR": "/tmp/pv-journal-test/"]).path == "/tmp/pv-journal-test")
        check(PiJobTracker.journalDirectory(environment: [:]) == FileManager.default.temporaryDirectory)
        check(PiJobTracker.journalDirectory(environment: ["TMPDIR": "  "]) == FileManager.default.temporaryDirectory)
        PiJobTracker.appendJournal(directory: dir, line: line)
        PiJobTracker.appendJournal(directory: dir, line: String(repeating: "x", count: 100), maxBytes: 64)
        let logged = (try? String(contentsOf: dir.appendingPathComponent(PiJobTracker.journalFileName), encoding: .utf8)) ?? ""
        check(logged.count <= 128, "oversize journal trims back, got \(logged.count)")
        try? FileManager.default.removeItem(at: dir)
    }
}
